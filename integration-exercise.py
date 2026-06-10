#!/usr/bin/env python3

import csv
import json
import sys
import argparse
import requests
from io import StringIO
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any, Optional
import boto3
from botocore.exceptions import ClientError
from bs4 import BeautifulSoup

INITIAL_HTML_URL = "https://bitbucket.org/cityhive/jobs/src/master/integration-eng/integration-entryfile.html"


class InventoryProcessor:
    def __init__(self):
        self.s3_client = boto3.client('s3')
        self.duplicate_skus = set()
        self.itemnum_counts = defaultdict(int)

    def get_s3_details_from_html(self, html_url: str) -> tuple:
        try:
            response = requests.get(html_url)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            bucket_div = soup.find('div', id='bucket-value')
            bucket = bucket_div.text.strip() if bucket_div else None
            
            region_div = soup.find('div', id='region-value')
            region = region_div.get('data-region') if region_div else None
            
            path_spans = soup.select('#object-value .path')
            object_path = ''.join(span.text for span in path_spans) if path_spans else None
            
            if not all([bucket, region, object_path]):
                raise Exception("Could not extract S3 details from HTML")
            
            print(f"Found S3 details - Bucket: {bucket}, Region: {region}, Path: {object_path}")
            return bucket, region, object_path
            
        except Exception as e:
            print(f"Error parsing HTML: {e}", file=sys.stderr)
            sys.exit(1)

    def download_from_s3(self, bucket: str, key: str, region: str = None) -> str:
        try:
            if region:
                s3_client = boto3.client('s3', region_name=region)
            else:
                s3_client = self.s3_client
                
            response = s3_client.get_object(Bucket=bucket, Key=key)
            content = response['Body'].read().decode('utf-8')
            print(f"Successfully downloaded from S3: {bucket}/{key}")
            return content
        except ClientError as e:
            print(f"Error downloading from S3: {e}", file=sys.stderr)
            sys.exit(1)

    def parse_csv_content(self, content: str) -> List[Dict[str, Any]]:
        csv_file = StringIO(content)
        reader = csv.DictReader(csv_file)
        records = list(reader)
        
        for record in records:
            item_num = record.get('ItemNum', '')
            if item_num:
                self.itemnum_counts[item_num] += 1
        
        for item_num, count in self.itemnum_counts.items():
            if count > 1:
                self.duplicate_skus.add(item_num)
        
        return records

    def calculate_new_price(self, cost: float, price: float) -> float:
        if cost == 0:
            margin = 0
        else:
            margin = (price - cost) / cost
        
        if margin > 0.30:
            increase = 0.07
        else:
            increase = 0.09
        
        new_price = price * (1 + increase)
        return round(new_price, 2)

    def process_upc_or_internal_id(self, upc: str, record_id: str) -> tuple:
        if upc and upc.isdigit() and len(upc) > 5:
            return upc, None
        else:
            return None, f"biz_id_{record_id}"

    def get_tags(self, margin: float, is_duplicate_sku: bool) -> List[str]:
        tags = []
        if is_duplicate_sku:
            tags.append("duplicate_sku")
        if margin > 0.30:
            tags.append("high_margin")
        elif margin < 0.30:
            tags.append("low_margin")
        return tags

    def transform_record(self, record: Dict[str, str], index: int) -> Optional[Dict[str, Any]]:
        try:
            cost = float(record.get('Cost', 0))
            price = float(record.get('Price', 0))
            quantity = int(record.get('Quantity', 0))
            
            sale_date = record.get('SaleDate', '')
            if sale_date:
                try:
                    sale_year = datetime.strptime(sale_date, '%Y-%m-%d').year
                    if sale_year != 2020:
                        return None
                except ValueError:
                    if '2020' not in sale_date:
                        return None
            
            if cost == 0:
                margin = 0
            else:
                margin = (price - cost) / cost
            
            upc = record.get('UPC', '')
            upc_value, internal_id = self.process_upc_or_internal_id(upc, index)
            
            new_price = self.calculate_new_price(cost, price)
            
            item_num = record.get('ItemNum', '')
            is_duplicate = item_num in self.duplicate_skus
            
            tags = self.get_tags(margin, is_duplicate)
            
            properties = {
                "department": record.get('Department', ''),
                "vendor": record.get('Vendor', ''),
                "description": record.get('Description', '')
            }
            
            transformed = {
                "upc": upc_value,
                "internal_id": internal_id,
                "price": new_price,
                "name": f"{record.get('Item', '')} {record.get('ItemExtra', '')}".strip(),
                "department": record.get('Department', ''),
                "properties": json.dumps(properties),
                "tags": tags,
                "quantity": quantity,
                "cost": cost,
                "original_price": price,
                "margin": margin
            }
            
            return transformed
            
        except (ValueError, KeyError) as e:
            print(f"Error processing record {index}: {e}", file=sys.stderr)
            return None

    def process_and_transform(self, content: str) -> List[Dict[str, Any]]:
        records = self.parse_csv_content(content)
        transformed_records = []
        
        for idx, record in enumerate(records):
            transformed = self.transform_record(record, idx)
            if transformed:
                transformed_records.append(transformed)
        
        return transformed_records

    def save_to_csv(self, records: List[Dict[str, Any]], output_file: str):
        if not records:
            print("No records to save", file=sys.stderr)
            return
        
        fieldnames = ['upc', 'internal_id', 'price', 'name', 'department', 
                     'properties', 'tags', 'quantity']
        
        with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            
            for record in records:
                row = {
                    'upc': record.get('upc', ''),
                    'internal_id': record.get('internal_id', ''),
                    'price': record.get('price', 0),
                    'name': record.get('name', ''),
                    'department': record.get('department', ''),
                    'properties': record.get('properties', '{}'),
                    'tags': ','.join(record.get('tags', [])),
                    'quantity': record.get('quantity', 0)
                }
                writer.writerow(row)
        
        print(f"Saved {len(records)} records to {output_file}")


class InventoryAPIClient:
    def __init__(self, api_url: str = "http://localhost:3000"):
        self.api_url = api_url
        self.session = requests.Session()

    def upload_inventory(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = {"inventory_units": records}
        
        try:
            response = self.session.post(
                f"{self.api_url}/inventory_uploads.json",
                json=payload
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error uploading to API: {e}", file=sys.stderr)
            sys.exit(1)

    def list_uploads(self) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(f"{self.api_url}/inventory_uploads.json")
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error listing uploads: {e}", file=sys.stderr)
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Inventory Processing Tool')
    parser.add_argument('command', choices=['generate_csv', 'upload', 'list_uploads'])
    parser.add_argument('--input-file', help='Input CSV file path')
    parser.add_argument('--output-file', default='inventory_output.csv')
    parser.add_argument('--api-url', default='http://localhost:3000')
    parser.add_argument('--s3-bucket', help='S3 bucket name (optional - will auto-discover from HTML if not provided)')
    parser.add_argument('--s3-key', help='S3 object key (optional - will auto-discover from HTML if not provided)')
    
    args = parser.parse_args()
    
    processor = InventoryProcessor()
    api_client = InventoryAPIClient(args.api_url)
    
    if args.command == 'generate_csv':
        if args.input_file:
            with open(args.input_file, 'r', encoding='utf-8') as f:
                content = f.read()
            transformed = processor.process_and_transform(content)
            processor.save_to_csv(transformed, args.output_file)
        else:
            bucket, region, key = processor.get_s3_details_from_html(INITIAL_HTML_URL)
            content = processor.download_from_s3(bucket, key, region)
            transformed = processor.process_and_transform(content)
            processor.save_to_csv(transformed, args.output_file)
    
    elif args.command == 'upload':
        if args.input_file:
            with open(args.input_file, 'r', encoding='utf-8') as f:
                content = f.read()
        else:
            bucket, region, key = processor.get_s3_details_from_html(INITIAL_HTML_URL)
            content = processor.download_from_s3(bucket, key, region)
        
        transformed = processor.process_and_transform(content)
        result = api_client.upload_inventory(transformed)
        print(json.dumps(result, indent=2))
    
    elif args.command == 'list_uploads':
        uploads = api_client.list_uploads()
        print(json.dumps(uploads, indent=2))


if __name__ == "__main__":
    main()
