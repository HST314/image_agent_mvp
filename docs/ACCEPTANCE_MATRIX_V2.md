# Acceptance matrix v2

| Task | Production boundary | Automated evidence |
|---|---|---|
| T02 | policy consumer map; confirmed policy revision creates branch | `test_t02_*` |
| T03 | legacy/modern wheel metadata plus packaged runtime resources | wheel empty-venv script and `test_t03_*` |
| T04 | stable resource code/resource/trace/degradation contract | `test_t04_*` |
| T05 | `AssetStoreProtocol`; persistent success URI guard | `test_t05_*` |
| T07 | provider-owned timeout, zero SDK retry, unknown action projection | `test_t07_*` |
| T10 | deterministic WAL target and checkpoint/branch rollback | `test_t10_*` and crash injection |
