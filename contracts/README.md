# Portable contracts

`schemas/` 是 `export_contracts()` 的确定性输出，`openapi.json` 来自 public FastAPI app，`fixtures/` 是公共 golden corpus。浏览器或 Runtime 提供的跨语言样本应与这些版本保持一致。

从独立目录根运行 `./scripts/check-generated.sh` 会在临时目录重建全部 Schema/OpenAPI，并逐字节比较当前文件；需要更新时显式运行：

```bash
backend/.venv/bin/python scripts/generate-contracts.py --output-root contracts
```
