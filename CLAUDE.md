# Project Context

This repository is a take-home coding challenge for a job interview. The goal is to build an end-to-end MRR (Monthly Recurring Revenue) reporting pipeline backed by Stripe test data.

## Challenge Instructions

### Step 1: Setup & Data Generation (Python)
1. **Stripe Account**: Sign up for a free Stripe account (Developer/Test mode).
2. **Generate Data**: Write a Python script to populate the account with test data.
   - Create ~50–100 customers with varying subscription statuses (Active, Canceled, Past Due).
   - **Crucial**: Use Stripe **Test Clocks** to simulate the passage of time. We want to see a billing history of 6 months (not just data created "today").
   - Tip: Ask your AI agent to help write the Test Clock advancement logic to generate invoices across different months.

### Step 2: The Pipeline (Python -> BigQuery)
Extract the relevant data from Stripe and load it into Google BigQuery.
1. **GCP Account**: Create a free Google Cloud Platform account (includes $300 credit).
2. **Ingestion**: Write a script to fetch the necessary objects (Subscriptions, Invoices, Charges, or Events—you decide what is needed for MRR) and load them into BigQuery tables.
   - **Constraint**: Keep the schema simple. We care about the logic, not perfect normalization.

### Step 3: The Logic (BigQuery SQL)
MRR is not just "Revenue." It is the normalized monthly value of active recurring subscriptions.
1. Write a SQL query in BigQuery to calculate MRR for each month in the dataset.
2. Output should look like: `month | mrr_amount`.

### Step 4: The Visualization (React JS)
Build a lightweight frontend to display the data.
1. Create a simple React app.
2. Connect it to BigQuery (or a simple API endpoint wrapped around it).
3. Render a **Line Chart** showing the MRR trend over the 3–6 month period generated.

## Deliverables

A GitHub repository containing:

> **IMPORTANT**: Name the GitHub repo something obscure — like `dynamic-squirrel-3124`. Do not use `optisigns` or other identifiable words; it may leak the test to other candidates.

1. **README.md**: Instructions on how to run the data generator and start the app.
2. **/scripts**: Python data generation and ETL scripts.
3. **/sql**: The BigQuery SQL logic used to calculate MRR.
4. **/frontend**: The React application code.
5. A screenshot of the final dashboard.

## Working Agreements

- **Red / Green TDD** for all functionality — write a failing test first, make it pass, then refactor.
- **Folder structure**: Strictly follow the layout in the deliverables (`/scripts`, `/sql`, `/frontend`). Place new files under the correct top-level directory.
- **Update README.md** when instructions on how to run the data generator and start the app change. 
