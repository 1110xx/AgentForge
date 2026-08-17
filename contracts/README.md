#Portable contracts
schemas/ 是 export_contracts' 的确定性输出,openapi.json来自 pubLicFastAPI app,fixtures/是公共 galden corpus.浏览器或 Runtime 提供的
从独立目录根运行 ./scripts/check-generated.sh 会在临时目录重建全部Schema/OpenAPI,并逐字节比较当前文件;需要更新时显式运行:
""bash
backend/.venv/bin/python scripts/generate-contracts.py-output-roat contracts
