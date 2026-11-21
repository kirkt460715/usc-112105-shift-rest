# 联合班务核算资料约定

虚构的多点位轮班协议，点位使用 America/New_York，其记录包含秋季时钟回拨。区间包含起点、不含终点。

## operators

职工所属比较分组与岗位资格；点位列出的岗位只能由具有该岗位资质的人值守。

字段 `person`, `comparison_group`, `qualifications`, `timezone`。

## duties

签定班表及持续覆盖要求。mandatory 为全区间连续覆盖。

字段 `duty`, `person`, `site`, `role`, `start`, `stop`, `mandatory`。

## clock

记录的在岗及等候义务，签到状态可与借调记录相冲突。

字段 `record`, `person`, `site`, `start`, `stop`, `state`, `duty`。

## loans

点位带班确认的借调。confirmed 使原岗与新岗重叠覆盖不能双计。conflict 表示还无法确认当时哪个岗位有实际覆盖。

字段 `loan`, `person`, `original_duty`, `new_site`, `start`, `stop`, `evidence`。

## breaks

free 为实际自由可支配；standby 不满足自由休息；recall 的 recall_at 终止原休息。

字段 `break`, `person`, `duty`, `start`, `stop`, `evidence`, `recall_at`。

## agreements

window_hours 为连续义务的观察窗。required_free_minutes 是窗内必须有的连续自由休息。minimum_coverage 按职工组的可判定时间占比判断。dedup_group 相同且相交的不足范围仅提出一项补休，最低补休按该范围触发条款最高值。

字段 `rule`, `scope`, `window_hours`, `required_free_minutes`, `minimum_coverage`, `dedup_group`, `compensation_minutes`。

## next_shifts

下月岗位与允许的备选职工，仍需查人员资质。该表供候选班表使用，允许指定可执行的替班和补休。

字段 `shift`, `plan`, `site`, `role`, `start`, `stop`, `candidates`, `mandatory`。

## rest_promises

已签收补休时间与欠账。列出的原职工不能同时用于其他安排。

字段 `promise`, `person`, `start`, `stop`, `source_duty`。
