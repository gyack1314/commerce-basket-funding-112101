# 联购优惠的留存收益与商家共同出资核定

本地经营应用，回答跨店满减活动结束后的两类问题：**这次优惠留下了多少真实
收益**，以及**每家商户按约应承担多少让利**。金额归属的确定性核定与净增
收益的统计推断分离处理；全部为本地合成资料与内部预算，不接真实支付。

## 能力

* **交易持续导入**：追加式事实仓库，支持多资料包、更正链与 as-of 截点查询。
* **篮子核定**：店铺折扣→平台券→跨店补贴顺序分摊、门槛失效、退货按件
  冲回、消费者应补/待追收、四类金额归属与强制守恒（见
  [架构说明](docs/architecture.md)）。
* **费用确认与异议**：已确认出资不被无声重算，差额显式挂账；争议只冻结
  受影响范围，异议成立整篮重算并联动其他商家分摊基数。
* **收益分析**：app 渠道可比人群匹配 + 自助法区间；web 无对照人群的效应
  标记为不可识别，保留不认定部分。
* **方案与预算**：历史支持范围标注、争议预留、独立/合并可行性与实际缺口。
* **版本与溯源**：核定结果版本化，可 diff 后补售后的影响，可从任一核定
  追到原篮子、合同与当时争议状态。

## 环境与运行

Python 3.11，依赖固定在 `requirements.txt`。命令以隔离模式运行，资料路径
显式传入：

```sh
python -m pip install -r requirements.txt

python -I run.py ingest
python -I run.py economics --basket b0005
python -I run.py ledger
python -I run.py analyze --bootstrap 1000
python -I run.py budget
python -I run.py trace --basket b0005
python -I run.py run-all --run-id current --output output/current
```

全局参数 `--data`、`--cutoff`、`--state` 放在子命令之前，例如：

```sh
python -I run.py --cutoff 2026-06-20T10:00:00+00:00 determine --run-id early
python -I run.py diff --old-run early --new-run current
python -I run.py --cutoff 2026-06-20T10:00:00+00:00 claim \
    --claim-id claim-open --decision rejected --evidence "合同成本单价4592"
```

## 测试

```sh
python -I -m unittest discover -s tests -v
python -I -m compileall -q app run.py tests
```

测试覆盖整数分摊与余分、顺序分摊与门槛失效、退货冲回与待追收、确认差额
与争议冻结、可识别范围与区间、预算缺口、版本 diff 与溯源，并含合成最小
场景覆盖真实资料未触发的边界。

## 资料与状态

* `data/history.json` 为首批事实，`data/updates.json` 为后补资料，字段与
  金额含义见 [资料约定](docs/input-contract.md)。
* 运行产生的核定版本在 `state/`，汇总工件在 `output/`，两者不入库版本控制。
* 容器方式：`docker build -t basket-funding-local . && docker run --rm basket-funding-local`。
