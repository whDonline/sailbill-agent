.PHONY: help install samples demo test lint eval trace clean

PY ?= python
VENV ?= .venv
BIN := $(VENV)/bin
ifeq ($(OS),Windows_NT)
BIN := $(VENV)/Scripts
endif

help:
	@echo "make install   安装依赖（可编辑模式）"
	@echo "make samples   生成合成样本"
	@echo "make demo      跑通全链路并打印结果表"
	@echo "make test      单元测试（离线）"
	@echo "make lint      ruff 检查"
	@echo "make eval      金标评测（含门禁）"
	@echo "make trace     生成 out/records.json 与 out/trace.json"

install:
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e ".[dev,llm]"

samples:
	$(BIN)/python scripts/make_samples.py

demo: samples
	$(BIN)/python -m sailbill demo

test: samples
	$(BIN)/python -m pytest -q

lint:
	$(BIN)/python -m ruff check src tests harness scripts

eval: samples
	$(BIN)/python harness/run_eval.py --min-score 0.95 --out eval/report.md

trace: samples
	$(BIN)/python -m sailbill run samples --store samples/existing_bills.json \
		--out out/records.json --trace out/trace.json --silent 2>/dev/null || \
	$(BIN)/python -m sailbill run samples --store samples/existing_bills.json \
		--out out/records.json --trace out/trace.json --quiet

clean:
	rm -rf out .cache build dist *.egg-info .pytest_cache
