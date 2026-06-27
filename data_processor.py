import pandas as pd
from scipy import stats
import numpy as np
from datetime import datetime, timedelta, date

from database import engine


def generate_weekly_report(user_id: int) -> str:
    """Generate a simple 7-day nutrition trend report for a single user."""
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=7)

    query = (
        "SELECT log_id, user_id, timestamp, entry_type, description, calories, protein, carbs, fat, amount, unit "
        "FROM logs WHERE user_id = :user_id AND timestamp >= :start_date AND timestamp <= :end_date "
        "ORDER BY timestamp ASC"
    )

    df = pd.read_sql_query(
        query,
        con=engine,
        params={
            "user_id": user_id,
            "start_date": start_date.isoformat(sep=' '),
            "end_date": end_date.isoformat(sep=' '),
        },
        parse_dates=["timestamp"],
    )

    if df.empty:
        return "I couldn't find any logs for the last 7 days. Start logging meals or activities and try /report again."

    df["date"] = df["timestamp"].dt.date
    summary = df.groupby("date").agg(
        net_calories=("calories", "sum"),
        food_calories=("calories", lambda s: s[df.loc[s.index, "entry_type"] == "food"].sum()),
        activity_calories=("calories", lambda s: s[df.loc[s.index, "entry_type"] == "activity"].sum()),
        protein=("protein", "sum"),
        carbs=("carbs", "sum"),
        fat=("fat", "sum"),
        entries=("log_id", "count"),
    ).reset_index()

    average_net = summary["net_calories"].mean()
    total_food = summary["food_calories"].sum()
    total_activity = summary["activity_calories"].sum()
    total_protein = summary["protein"].sum()
    total_carbs = summary["carbs"].sum()
    total_fat = summary["fat"].sum()

    top_calorie_days = summary.sort_values("net_calories", ascending=False).head(3)
    top_days_text = "\n".join(
        f"{row['date']}: {row['net_calories']} kcal" for _, row in top_calorie_days.iterrows()
    )

    report = (
        f"Weekly nutrition report ({start_date.date()} to {end_date.date()}):\n"
        f"Average net calories per day: {average_net:.1f} kcal\n"
        f"Total food calories: {total_food:.1f} kcal\n"
        f"Total activity calories burned: {total_activity:.1f} kcal\n"
        f"Total macros: Protein {total_protein:.1f} g, Carbs {total_carbs:.1f} g, Fat {total_fat:.1f} g\n"
        f"Logged days: {summary.shape[0]}\n"
        f"Top calorie days:\n{top_days_text}"
    )
    return report


def process_and_analyze_data(input_file1_path: str, input_file2_path: str, output_file_path: str):
    """
    Loads, cleans, processes, and analyzes data from two CSV files.

    Args:
        input_file1_path: Path to the first input CSV file (e.g., main features).
        input_file2_path: Path to the second input CSV file (e.g., auxiliary/test scores).
        output_file_path: Path where the final processed DataFrame will be saved.
    """
    print("--- Starting Data Processing Pipeline ---")

    # 1. Load Data
    try:
        df1 = pd.read_csv(input_file1_path)
        print(f"Successfully loaded File 1 from: {input_file1_path} (Shape: {df1.shape})")
    except FileNotFoundError:
        print(f"Error: File not found at {input_file1_path}. Please check the path.")
        return None
    except Exception as e:
        print(f"An error occurred loading File 1: {e}")
        return None

    try:
        df2 = pd.read_csv(input_file2_path)
        print(f"Successfully loaded File 2 from: {input_file2_path} (Shape: {df2.shape})")
    except FileNotFoundError:
        print(f"Error: File not found at {input_file2_path}. Please check the path.")
        return None
    except Exception as e:
        print(f"An error occurred loading File 2: {e}")
        return None

    # --- Data Cleaning and Transformation (Based on assumed column names) ---

    # Assuming 'ID' is the common key for merging
    if 'ID' not in df1.columns or 'ID' not in df2.columns:
        print("Warning: Neither DataFrame contains a common 'ID' column. Merging might fail.")
        # In a real scenario, we would prompt the user for the actual ID columns.
        merge_key = None
    else:
        merge_key = 'ID'

    # Merge dataframes on the assumed key
    try:
        df_merged = pd.merge(df1, df2, on=merge_key, how='left')
        print(f"Data merged successfully on '{merge_key}' (Shape: {df_merged.shape})")
    except Exception as e:
        print(f"Error during merge operation: {e}")
        return None

    # 2. Handling Missing Values
    print("\n--- Data Cleaning ---")
    initial_missing = df_merged.isnull().sum().sum()
    if initial_missing > 0:
        # Simple imputation strategy: Fill missing numeric values with the mean of that column,
        # and missing categorical values with 'Unknown'.
        for col in df_merged.columns:
            if df_merged[col].dtype in ['float64', 'int64']:
                df_merged[col] = df_merged[col].fillna(df_merged[col].mean())
            else:
                df_merged[col] = df_merged[col].fillna('Unknown')
        print("Missing values imputed using mean (numeric) or 'Unknown' (categorical).")

    # 3. Feature Engineering / Derived Calculations
    print("\n--- Feature Engineering ---")

    # Example: Create a combined score based on two existing metrics
    if 'Score_A' in df_merged.columns and 'Test_B' in df_merged.columns:
        df_merged['Combined_Score'] = (df_merged['Score_A'] * 0.6) + (df_merged['Test_B'] * 0.4)
        print("Created 'Combined_Score'.")

    # 4. Statistical Analysis Example (Correlation Check)
    print("\n--- Statistical Analysis ---")
    numerical_cols = df_merged.select_dtypes(include=np.number).columns.tolist()
    if len(numerical_cols) >= 2:
        # Calculate correlation matrix for available numerical features
        correlation_matrix = df_merged[numerical_cols].corr()
        print("--- Correlation Matrix (Head): ---")
        print(correlation_matrix.head())

        # Example T-test between two hypothetical groups/features if they exist
        if 'Score_A' in numerical_cols and 'Combined_Score' in numerical_cols:
            try:
                stats.ttest_ind(df_merged['Score_A'], df_merged['Combined_Score'], equal_var=False)
                print("Performed T-test between Score_A and Combined_Score.")
            except Exception as e:
                print(f"Could not perform statistical test: {e}")

    # 5. Final Output Preparation (Dropping intermediate columns if necessary)
    final_df = df_merged.copy() # Keep a copy or select only required columns

    # --- Save Result ---
    try:
        final_df.to_csv(output_file_path, index=False)
        print("\n=============================================")
        print("✅ SUCCESS! The processed data has been saved.")
        print(f"Output file path: {output_file_path}")
        print("=============================================")
    except Exception as e:
        print(f"\n❌ CRITICAL ERROR saving the file: {e}")


if __name__ == "__main__":
    # !!! IMPORTANT USER CONFIGURATION REQUIRED !!!
    # Please replace these placeholders with the actual paths to your files.
    FILE1 = "./data/input_features.csv"  # e.g., Student demographics and raw scores
    FILE2 = "./data/test_results.csv"     # e.g., Specialized assessment results
    OUTPUT = "./processed_analysis_output.csv" # The desired output file name

    process_and_analyze_data(
        input_file1_path=FILE1,
        input_file2_path=FILE2,
        output_file_path=OUTPUT
    )