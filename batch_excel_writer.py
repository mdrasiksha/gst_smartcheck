import pandas as pd

def write_batch_summary(results, output_path):
    df = pd.DataFrame(results)
    df.to_excel(output_path, index=False)
