.PHONY: extract-layout extract-layout-check

extract-layout:
	python scripts/extract_tab_layout.py

extract-layout-check:
	python scripts/extract_tab_layout.py --check
