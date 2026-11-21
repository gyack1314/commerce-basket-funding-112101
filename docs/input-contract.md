# 资料约定

金额均为人民币分；unit_price是优惠前单件售价，unit_cost为商家单件成本。line_id在篮内唯一；discounts的eligible_lines指定适用行，threshold以这些行的优惠前金额判断，amount为整篮优惠额，merchant_share按合同中参与商家的权重承担，平台承担余量。店铺折扣先于平台券，跨店补贴最后；各步仅以尚未优惠的正金额为分摊权重，余分按line_id字典序分配。售后return_qty为累计退回数量，confirmed标识已核实事实；取消与退货均减少留存量，goodwill仅为额外现金补偿。跨店补贴在剩余适用金额不足threshold时全额失效，消费者应补部分抵减本次应退金额，最低退款为零，无法抵减部分单列待追收，不能当作已收。补贴失效时原出资按原归属冲回。claims的accepted事实只替换其target字段，pending不是有效更正；confirmed_funding只约束指定商家、篮子和费用范围。cohort覆盖活动前即确定的全部可参与账户，participated为实际参加，observations记录含零购买的同期经济结果。未来方案只有offers给定范围，不推断任意新门槛的确定收益；所有资金数值是内部预算，不接真实支付。

## 文件与时间

所有数据都是本地合成记录，不代表新闻中的企业或交易。history.json为首批事实，updates.json为后续补充资料；后者可引用前者的对象。id标识一条事实；revises引用被更正的事实身份，不表示删除历史。known_at、recorded_at、published_at等字段表示资料可获知时间，occurred_at或effective_at表示实际发生或适用时间，二者不可互换。时间使用带时区的ISO 8601字符串，区间无特别说明均含起点、不含终点。历史分析只能采用指定截点已可得资料；空列表、null、数值零分别保留其含义。合成金额不触及真实账户。

## 数据表

### baskets

字段：id, account_id, paid_at, known_at, mature_at, campaign。

### lines

字段：id, line_id, basket_id, merchant_id, quantity, unit_price, unit_cost, category。

### discounts

字段：id, basket_id, kind, eligible_lines, amount, threshold, merchant_share。

### cohort

字段：id, participated, channel, tenure_days, eligible_from, category。

### observations

字段：id, account_id, period_start, period_end, purchase_baskets, pre_period_spend, acquisition_cost。

### returns

字段：id, line_id, return_qty, confirmed, goodwill, occurred_at, known_at。

### claims

字段：id, basket_id, merchant_id, target, target_id, proposed, status, known_at。

### confirmed_funding

字段：id, basket_id, merchant_id, discount_id, amount, confirmed_at。

### budgets

字段：id, currency, cash_available, disputed。

### offers

字段：id, threshold, amount, platform_budget, eligible_categories。

### accepted_plans

字段：id, platform_budget, status, accepted_at。

input-schema.json列出资料中的字段类型概况和必要外键；同一字段可能存在null值，类型概况不是严格JSON Schema。后续资料可更正事实，但原记录可按其身份回查。

## 补充领域含义

同篮同商品的return_qty是该条获知时点的累计数量，多次资料不作数量相加；accepted且target指向同一事实的更正按known_at生效。claims中pending不修改原事实。confirmed_funding的amount是已确认商家出资金额，非已付款；争议和确认范围以basket_id、merchant_id、discount_id共同界定。预算接受以明确的platform_budget占用，与待追收分开。
