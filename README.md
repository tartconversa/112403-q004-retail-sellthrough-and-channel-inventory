# 线下渠道动销识别与压货风险预警

合成经销商收货、门店POS、库存及调拨事件样例，数量以件计。

All example identifiers and values are synthetic. CSV files use UTF-8. The first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `stores.csv` contains 门店层级和所属地区。 Columns are `store`, `parent`, `region`, `state`.
- `events.csv` contains 经销收货、门店交易、退货和调拨。 Columns are `event`, `store`, `sku`, `kind`, `qty`, `at`.
- `stock.csv` contains 门店和区域仓的期末库存快照。 Columns are `location`, `sku`, `qty`, `day`.

Python 3 standard library is sufficient to inspect these inputs. Run `python3 -m unittest discover -s tests -v` to check the delivered file structures and example references.
