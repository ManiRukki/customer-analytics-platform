import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine

st.set_page_config(page_title="Customer Analytics", layout="wide")
st.title("📊 Customer Analytics Platform")

DB_URI = "sqlite:///customer_analytics.db"

@st.cache_data
def load_data():
    engine = create_engine(DB_URI)
    return pd.read_sql("SELECT * FROM customers", engine)

try:
    df = load_data()
except:
    st.error("Run: python etl/load_data.py first")
    st.stop()

# Filters
regions = st.sidebar.multiselect("Region", df['region'].unique(), df['region'].unique())
filtered_df = df[df['region'].isin(regions)]

# Metrics
col1, col2, col3 = st.columns(3)
col1.metric("Total", len(filtered_df))
col2.metric("Active", len(filtered_df[filtered_df['status']=='active']))
col3.metric("Revenue", f"${filtered_df['revenue'].sum():,.0f}")

# Chart
fig = px.bar(filtered_df.groupby('region')['revenue'].sum().reset_index(), 
             x='region', y='revenue', color='region')
st.plotly_chart(fig, use_container_width=True)

st.dataframe(filtered_df)
