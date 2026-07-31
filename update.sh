mv ../*.zip .
unzip *.zip
rm *.zip
uv sync
uv run pytest
