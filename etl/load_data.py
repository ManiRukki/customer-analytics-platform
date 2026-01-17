import pandas as pd
from sqlalchemy import create_engine
import os

DB_URI = "sqlite:///customer_analytics.db"

def load_customer_data():
    print("🔄 Starting ETL process...")
    # Ensure data directory exists relative to script execution or fixed path
    # Assuming running from root
    csv_path = os.path.join('data', 'sample_customers.csv')
    
    if not os.path.exists(csv_path):
        print(f"❌ Error: {csv_path} not found.")
        return

    df = pd.read_csv(csv_path)
    print(f"📁 Loaded {len(df)} records")
    
    engine = create_engine(DB_URI)
    df.to_sql('customers', engine, if_exists='replace', index=False)
    print(f"✅ Loaded to database")

if __name__ == "__main__":
    load_customer_data()
