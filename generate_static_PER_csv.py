import argparse

from generate_static_chips_v3 import (
    build_daily_valuation,
    load_stock_list,
    resolve_csv_file,
    resolve_valuation_output_file,
)


def main():
    parser = argparse.ArgumentParser(
        description="Refresh daily PER valuation fields.")
    parser.add_argument("--output", default=None,
                        help="Valuation output file.")
    parser.add_argument("--csv-file", default=None,
                        help="Stock list file.")
    args = parser.parse_args()

    stocks = load_stock_list(resolve_csv_file(args.csv_file))
    build_daily_valuation(
        stocks,
        resolve_valuation_output_file(args.output),
    )


if __name__ == "__main__":
    main()
