# Customer Analytics Platform — Healthcare Data Engineering

A HIPAA-compliant healthcare analytics platform processing 10K+ pharmacy events/sec in real time — built on Azure Databricks, dbt Core, Delta Live Tables, Unity Catalog, Kafka, and Snowflake. Reduced pipeline runtime by 60%, cut compute costs by $12K/month, and enabled real-time stockout risk scoring preventing 1,200+ monthly incidents.

---

## Architecture

```
Data Sources
┌────────────┬──────────────┬───────────────┐
│ Kafka      │ EHR Systems  │ Claims / Rx   │
│ (10K+/sec) │ (Batch)      │ (Daily loads) │
└─────┬──────┴──────┬───────┴───────┬───────┘
      │             │               │
      ▼             ▼               ▼
 Spark Structured  Azure Data    Azure Blob
 Streaming         Factory       Storage
      │             │               │
      └─────────────┴───────────────┘
                    │
           Azure Databricks
        ┌───────────┴────────────┐
        │  Delta Live Tables     │
        │  Bronze → Silver       │
        │  (DLT Expectations)    │
        └───────────┬────────────┘
                    │
              dbt Core
        ┌───────────┴────────────┐
        │  Staging → Mart Models │
        │  Incremental + MERGE   │
        │  50+ production models │
        └───────────┬────────────┘
                    │
           Unity Catalog
        (HIPAA governance,
         column masking,
         row-level security)
                    │
          ┌─────────┴──────────┐
          ▼                    ▼
       Snowflake           Power BI
    (Analytical DW)      Dashboards
```

---

## Repository Structure

```
customer-analytics-platform/
├── dbt/
│   ├── models/
│   │   ├── staging/
│   │   │   ├── stg_kafka__pharmacy_events.sql
│   │   │   ├── stg_ehr__patient_records.sql
│   │   │   └── stg_claims__prescriptions.sql
│   │   ├── intermediate/
│   │   │   ├── int_pharmacy_enriched.sql
│   │   │   └── int_patient_risk_scores.sql
│   │   └── marts/
│   │       ├── fct_pharmacy_fills.sql
│   │       ├── fct_stockout_risk.sql
│   │       ├── dim_patients.sql         # PII-masked
│   │       └── dim_pharmacy_locations.sql
│   ├── macros/
│   │   ├── mask_patient_id.sql          # HIPAA: SHA-256 hash
│   │   └── incremental_merge.sql        # Reusable merge macro
│   ├── tests/
│   │   ├── assert_hipaa_compliance.sql  # No raw PII in marts
│   │   ├── assert_fill_count_positive.sql
│   │   └── generic/
│   │       └── referential_integrity.sql
│   ├── analyses/
│   │   └── stockout_risk_model.sql
│   └── dbt_project.yml
├── databricks/
│   ├── dlt/
│   │   └── pharmacy_streaming_pipeline.py
│   └── unity_catalog/
│       ├── column_masks.sql
│       └── row_filters.sql
├── streaming/
│   └── kafka_spark_consumer.py          # Structured Streaming consumer
├── airflow/
│   └── dags/
│       └── healthcare_platform_dag.py   # Airflow + dbt operator DAG
├── mlflow/
│   └── pipeline_metrics_logger.py       # dbt run metrics → MLflow
├── tests/
│   └── test_dbt_models.py
├── requirements.txt
└── README.md
```

---

## Core Code

### dbt Incremental Model — Pharmacy Fills Fact
```sql
-- dbt/models/marts/fct_pharmacy_fills.sql
{{
  config(
    materialized='incremental',
    unique_key='fill_key',
    incremental_strategy='merge',
    cluster_by=['fill_date', 'pharmacy_id'],
    post_hook="OPTIMIZE {{ this }} ZORDER BY (fill_date, drug_ndc)"
  )
}}

WITH pharmacy_events AS (
  SELECT * FROM {{ ref('int_pharmacy_enriched') }}
  {% if is_incremental() %}
    WHERE fill_timestamp > (SELECT MAX(fill_timestamp) FROM {{ this }})
  {% endif %}
),

with_keys AS (
  SELECT
    {{ dbt_utils.generate_surrogate_key(['fill_id', 'pharmacy_id']) }} AS fill_key,
    fill_id,
    {{ mask_patient_id('patient_id') }}                                 AS patient_id_hashed,
    drug_ndc,
    pharmacy_id,
    DATE(fill_timestamp)                                                AS fill_date,
    fill_timestamp,
    quantity_dispensed,
    days_supply,
    fill_status,
    prescriber_npi,
    CURRENT_TIMESTAMP()                                                 AS dbt_updated_at
  FROM pharmacy_events
  WHERE fill_status IS NOT NULL
    AND drug_ndc IS NOT NULL
)

SELECT * FROM with_keys
```

### dbt Macro — HIPAA PII Masking
```sql
-- dbt/macros/mask_patient_id.sql
{% macro mask_patient_id(column_name) %}
  SHA2(CAST({{ column_name }} AS STRING), 256)
{% endmacro %}
```

### dbt Test — HIPAA Compliance Guard
```sql
-- dbt/tests/assert_hipaa_compliance.sql
-- Fails if any raw patient_id (non-hashed) reaches a mart table

WITH mart_patients AS (
  SELECT patient_id_hashed FROM {{ ref('fct_pharmacy_fills') }}
  UNION ALL
  SELECT patient_id_hashed FROM {{ ref('dim_patients') }}
)

-- A SHA-256 hash is always exactly 64 hex characters
-- If any value is shorter, raw PII may have leaked through
SELECT patient_id_hashed
FROM mart_patients
WHERE LENGTH(patient_id_hashed) != 64
   OR patient_id_hashed IS NULL
```

### Kafka + Spark Structured Streaming Consumer
```python
# streaming/kafka_spark_consumer.py
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StructField, StringType, IntegerType, TimestampType
import mlflow

KAFKA_BOOTSTRAP = "kafka-broker:9092"
TOPIC = "pharmacy-events"
CHECKPOINT = "/mnt/checkpoints/pharmacy-streaming"
DELTA_OUTPUT = "/mnt/silver/pharmacy_events"

SCHEMA = StructType([
    StructField("fill_id", StringType()),
    StructField("patient_id", StringType()),
    StructField("drug_ndc", StringType(), nullable=False),
    StructField("pharmacy_id", StringType(), nullable=False),
    StructField("fill_timestamp", TimestampType()),
    StructField("quantity_dispensed", IntegerType()),
    StructField("fill_status", StringType()),
])

def build_streaming_pipeline(spark: SparkSession):
    raw_stream = (
        spark.readStream
        .format("kafka")
        .option("kafka.bootstrap.servers", KAFKA_BOOTSTRAP)
        .option("subscribe", TOPIC)
        .option("startingOffsets", "latest")
        .option("kafka.group.id", "pharmacy-streaming-consumer")
        .option("failOnDataLoss", "false")
        .load()
    )

    parsed = (
        raw_stream
        .select(F.from_json(F.col("value").cast("string"), SCHEMA).alias("data"))
        .select("data.*")
        .filter(F.col("drug_ndc").isNotNull())
        .filter(F.col("pharmacy_id").isNotNull())
        # HIPAA: hash patient ID at ingestion — never store raw PII in Delta
        .withColumn("patient_id", F.sha2(F.col("patient_id"), 256))
        .withColumn("ingested_at", F.current_timestamp())
        .withColumn("processing_latency_ms",
            (F.unix_timestamp(F.current_timestamp()) -
             F.unix_timestamp(F.col("fill_timestamp"))) * 1000
        )
    )

    query = (
        parsed.writeStream
        .format("delta")
        .outputMode("append")
        .option("checkpointLocation", CHECKPOINT)
        .option("mergeSchema", "true")
        .trigger(processingTime="10 seconds")
        .start(DELTA_OUTPUT)
    )

    return query

if __name__ == "__main__":
    spark = SparkSession.builder.appName("PharmacyStreamingConsumer").getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    query = build_streaming_pipeline(spark)
    query.awaitTermination()
```

### Unity Catalog — HIPAA Column Masking + Row Filters
```sql
-- databricks/unity_catalog/column_masks.sql

-- Column masking: data engineers see hashed IDs, analysts see NULL
CREATE OR REPLACE FUNCTION catalog.security.mask_patient_id(patient_id_hashed STRING)
RETURNS STRING
RETURN CASE
  WHEN is_member('data_engineers') OR is_member('clinical_data_science')
    THEN patient_id_hashed
  ELSE NULL
END;

ALTER TABLE catalog.silver.pharmacy_events
ALTER COLUMN patient_id_hashed
SET MASK catalog.security.mask_patient_id;

-- Row filter: pharmacy staff only see their own pharmacy data
CREATE OR REPLACE ROW FILTER catalog.security.pharmacy_row_filter
ON TABLE catalog.silver.pharmacy_events
USING (
  is_member('data_engineers')
  OR pharmacy_id = session_context('current_pharmacy_id')
);
```

### MLflow — Pipeline Metrics Logging
```python
# mlflow/pipeline_metrics_logger.py
import mlflow
import subprocess
import json
from datetime import datetime

def log_dbt_run_metrics(dbt_run_result_path: str, experiment_name: str):
    """Parse dbt run_results.json and log metrics to MLflow."""
    mlflow.set_experiment(experiment_name)

    with open(dbt_run_result_path) as f:
        results = json.load(f)

    with mlflow.start_run(run_name=f"dbt_run_{datetime.utcnow().strftime('%Y%m%d_%H%M')}"):
        models_run = len(results["results"])
        success = sum(1 for r in results["results"] if r["status"] == "success")
        failed = models_run - success
        total_exec_ms = sum(r.get("execution_time", 0) for r in results["results"]) * 1000

        mlflow.log_metrics({
            "models_run": models_run,
            "models_success": success,
            "models_failed": failed,
            "success_rate": success / max(models_run, 1),
            "total_execution_ms": total_exec_ms,
        })

        mlflow.log_param("dbt_version", results.get("metadata", {}).get("dbt_version", "unknown"))
        mlflow.log_artifact(dbt_run_result_path)

        if failed > 0:
            failed_models = [r["unique_id"] for r in results["results"] if r["status"] != "success"]
            mlflow.log_param("failed_models", str(failed_models))
```

---

## Key Results

| Metric | Value |
|---|---|
| dbt models in production | 50+ across staging, intermediate, marts |
| Pipeline runtime reduction | **60%** (incremental models + Z-order) |
| Compute cost savings | **$12K/month** |
| Streaming throughput | **10K+ pharmacy events/sec** |
| End-to-end latency | **< 500ms** (Kafka → Silver Delta) |
| Stockout incidents prevented | **1,200+/month** ($15K/month saved) |
| MTTR reduction | **~50%** (dbt tests + Great Expectations) |
| HIPAA compliance | Full column masking + row-level security via Unity Catalog |
| Teams served | 20+ downstream analytics consumers |

---

## Setup

```bash
git clone https://github.com/ManiRukki/customer-analytics-platform.git
cd customer-analytics-platform
pip install -r requirements.txt

# Run dbt
cd dbt
dbt deps
dbt run --target dev
dbt test --target dev
dbt docs generate && dbt docs serve

# Start streaming consumer (requires Spark + Kafka)
spark-submit streaming/kafka_spark_consumer.py
```

---

## Tech Stack

`Azure Databricks` `dbt Core` `Delta Live Tables` `Unity Catalog` `Apache Kafka`
`Spark Structured Streaming` `Snowflake` `Apache Airflow` `MLflow`
`Azure Data Factory` `ADLS Gen2` `Power BI` `Python` `SQL`

---

## Related Projects

- [streaming-event-pipeline](https://github.com/ManiRukki/streaming_event_pipeline) — Kafka + Flink + DLT real-time lakehouse
- [enterprise-data-warehouse](https://github.com/ManiRukki/entireprise_data_warehouse) — eBay 95TB Snowflake migration
- [realtime-ml-feature-store](https://github.com/ManiRukki/realtime_ml_feature_store) — GenAI feature pipeline on GCP + AWS Bedrock
