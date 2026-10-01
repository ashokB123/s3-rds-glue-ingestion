import io
import os
from urllib.parse import quote_plus

import boto3
import pandas as pd
from sqlalchemy import create_engine

S3_BUCKET = os.environ["S3_BUCKET_NAME"]
CSV_KEY = os.environ["CSV_FILE_KEY"]
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

RDS_HOST = os.environ["RDS_HOST"]
RDS_USER = os.environ["RDS_USER"]
RDS_PASSWORD = os.environ["RDS_PASSWORD"]
RDS_DB = os.environ["RDS_DB_NAME"]
RDS_TABLE = os.environ["RDS_TABLE_NAME"]

GLUE_DB = os.environ["GLUE_DB_NAME"]
GLUE_TABLE = os.environ["GLUE_TABLE_NAME"]
GLUE_PREFIX = os.environ.get("GLUE_S3_PREFIX", "fallback-data/")

GLUE_TYPES = {"int64": "bigint", "float64": "double", "bool": "boolean"}


def read_csv_from_s3():
    print("Reading CSV from S3...")
    s3 = boto3.client("s3", region_name=REGION)
    obj = s3.get_object(Bucket=S3_BUCKET, Key=CSV_KEY)
    return pd.read_csv(
        io.BytesIO(obj["Body"].read()),
        dtype={"customer_zip_code_prefix": str},
    )


def load_to_rds(df):
    print("Connecting to RDS and uploading data...")
    url = (
        f"mysql+pymysql://{quote_plus(RDS_USER)}:{quote_plus(RDS_PASSWORD)}"
        f"@{RDS_HOST}:3306/{RDS_DB}"
    )
    engine = create_engine(url, connect_args={"connect_timeout": 10})
    # "replace" lets you re-run while testing without duplicate rows
    df.to_sql(RDS_TABLE, engine, if_exists="replace", index=False, chunksize=1000)
    with engine.connect() as conn:
        count = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {RDS_TABLE}").scalar()
    print(f"Data uploaded to RDS successfully. Rows in table: {count}")


def fallback_to_glue(df):
    print("Upload to RDS failed. Falling back to Glue...")
    s3 = boto3.client("s3", region_name=REGION)
    glue = boto3.client("glue", region_name=REGION)

    folder = f"{GLUE_PREFIX}{GLUE_TABLE}/"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{folder}data.csv",
        Body=df.to_csv(index=False).encode("utf-8"),
    )

    try:
        glue.get_database(Name=GLUE_DB)
    except glue.exceptions.EntityNotFoundException:
        glue.create_database(DatabaseInput={"Name": GLUE_DB})

    columns = [
        {"Name": c.lower(), "Type": GLUE_TYPES.get(str(t), "string")}
        for c, t in df.dtypes.items()
    ]
    table_input = {
        "Name": GLUE_TABLE,
        "TableType": "EXTERNAL_TABLE",
        "Parameters": {"classification": "csv", "skip.header.line.count": "1"},
        "StorageDescriptor": {
            "Columns": columns,
            "Location": f"s3://{S3_BUCKET}/{folder}",
            "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
            "SerdeInfo": {
                "SerializationLibrary": "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
                "Parameters": {"field.delim": ","},
            },
        },
    }
    try:
        glue.create_table(DatabaseName=GLUE_DB, TableInput=table_input)
    except glue.exceptions.AlreadyExistsException:
        glue.update_table(DatabaseName=GLUE_DB, TableInput=table_input)
    print("Fallback: Glue table created successfully.")


def main():
    df = read_csv_from_s3()
    try:
        load_to_rds(df)
    except Exception as e:
        print(f"Error uploading to RDS: {e}")
        fallback_to_glue(df)


if __name__ == "__main__":
    main()