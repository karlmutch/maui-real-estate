#!/usr/bin/env bash
set -euo pipefail

shopt -s nullglob

zip_files=( *.zip )

if (( ${#zip_files[@]} == 0 )); then
  echo "No .zip files found in $(pwd)" >&2
  exit 0
fi

for zip_file in "${zip_files[@]}"; do
  echo "Unzipping: $zip_file"
  unzip -o "$zip_file"
done

echo "Done. Unzipped ${#zip_files[@]} file(s)."
