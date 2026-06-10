## AWS S3 Setup (Working)

1. Created AWS account
2. Configured credentials: `aws configure`
3. Created bucket: `city-hive-justin-2024`
4. Uploaded file: `aws s3 cp test_data.csv s3://city-hive-justin-2024/inventory_data.csv`

## Run with S3

```bash
python integration-exercise.py generate_csv --s3-bucket city-hive-justin-2024 --s3-key inventory_data.csv
