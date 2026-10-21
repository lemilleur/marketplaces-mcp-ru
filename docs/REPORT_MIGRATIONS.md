# Current report sources

## Wildberries

Use `wb_get_order_feed` / `wb_order_feed` for current order statuses. The legacy
Statistics `/api/v1/supplier/orders` and `/sales` can omit orders whose payment
is not confirmed. An empty response does not establish zero activity.

The Order Feed is `POST https://seller-analytics-api.wildberries.ru/api/analytics/v1/order-feed`.
It requires Analytics permission and a period within the last 31 days. Dates
must be full RFC3339 with timezone. The period refers to CURRENT STATUS time,
not necessarily creation time. Paginate inside one snapshot using the returned
`data.snapshotTime`, offset and limit. This is not a historical event ledger.

For financial reconciliation use `wb_get_finance_report` / `wb_report_realization`:
`POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed`.
The retired v5 report no longer works. Request dates, limit and `rrdId` are in
the JSON body. Continue with the last row's `rrdId` until HTTP 204. Never stop
merely because a page is short. Requires Finance permission.

The new schema uses camelCase. Money fields such as `retailAmount` and `forPay`
are decimal strings: preserve precision with Decimal. `spp` is provided by WB.
Select actual sale/return rows; storage, delivery and other fees are separate
operations. Weekly/daily report availability differs from real-time orders.

Official references:
- https://dev.wildberries.ru/docs/openapi/analytics#tag/orderFeed
- https://dev.wildberries.ru/docs/openapi/reports
- https://dev.wildberries.ru/docs/openapi/financial-reports-and-accounting
- https://dev.wildberries.ru/release-notes?id=498

## Ozon

The old `/v3/finance/transaction/list` and `/totals` were retired; changing the
host does not restore them. The host remains `api-seller.ozon.ru`.

Use `ozon_get_finance_accruals` / `ozon_finance_transactions` for
`POST /v1/finance/accrual/by-day`, body `{date: YYYY-MM-DD, last_id: string}`.
Collect EVERY day in the requested period and every page until `last_id` is
empty. The new rows are in `accruals`, not `result.operations`.

`ozon_finance_accrual_types` is the category dictionary. `accrual_id` is not
itself a category identifier. Preserve signed decimal amounts, keep unknown
categories explicit, and never add total_amount to its own fee components.
Old precomputed totals are not returned by the new API.

Official retirement notice: https://t.me/s/OzonSellerAPI/441?q=%23operation
Current contract: https://docs.ozon.ru/api/seller/#operation/GetFinanceAccrualByDay
