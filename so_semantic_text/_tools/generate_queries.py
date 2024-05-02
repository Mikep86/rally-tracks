#!/usr/bin/env python3

import argparse
import bz2
import json


def get_open_func(filename: str):
    if filename.endswith(".bz2"):
        open_func = bz2.open
    else:
        open_func = open

    return open_func


def generate_queries(input_filename: str, output_filename: str, query_count: int):
    input_open_func = get_open_func(input_filename)
    output_open_func = get_open_func(output_filename)

    question_count = 0
    with (input_open_func(input_filename, mode="r") as input_file,
          output_open_func(output_filename, mode="w") as output_file):

        for line in input_file:
            parsed_line = json.loads(line)
            if parsed_line["type"] == "question":
                try:
                    output_file.write(parsed_line["title"])
                    output_file.write("\n")
                except TypeError:
                    output_file.write(parsed_line["title"].encode("utf-8"))
                    output_file.write(b"\n")

                question_count += 1
                if question_count % 100000 == 0:
                    print("Processed {} questions".format(question_count))

                if query_count is not None and question_count >= query_count:
                    break

        print("Processed {} questions in total".format(question_count))


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(
        description="Script to process stack overflow posts. Filters out non-question type posts and extracts title field.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    arg_parser.add_argument("input_file", help="Path to posts JSON file")
    arg_parser.add_argument("output_file", help="Path to output file")
    arg_parser.add_argument("-c", "--count", dest="query_count", type=int, required=False,
                            help="The number of queries to write to the output file. Defaults to all queries.")

    args = arg_parser.parse_args()
    generate_queries(args.input_file, args.output_file, args.query_count)
