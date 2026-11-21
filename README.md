# 联合班务核算

虚构的多点位轮班协议，点位使用 America/New_York，其记录包含秋季时钟回拨。区间包含起点、不含终点。

项目使用 Python 3.11.2 标准库。资料按来源表以 UTF-8 JSON Lines 保存，每行是一条记录。缺失值写作 `null`，其业务含义见资料约定。列名见 `data/manifest.json`，编号由各资料表原样保留。

[资料约定](docs/materials.md)说明各表的来源、时间及关联语义。从项目根目录可检查资料能否读取  

```sh
python3 inspect_inputs.py
python3 -m unittest discover -s tests -v
```
