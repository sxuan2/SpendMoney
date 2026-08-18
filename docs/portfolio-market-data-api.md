# Portfolio Market Data API

所有接口使用与 iPhone Shortcut 相同的用户 API Key，请发送请求头 X-API-Key。

## 获取需要报价的持仓证券

    curl -H "X-API-Key: YOUR_API_KEY" \
      https://YOUR_DOMAIN/spendmoney/api/portfolio/securities

默认只返回当前有持仓的证券。查询全部证券时增加 include_inactive=true。

自动化应保存返回的 security_id，不能只依赖 symbol，因为 GOOG USD 与 GOOG CAD 是两个证券。

## 提交证券价格

价格必须使用证券自己的币种，时间使用 ISO 8601：

    curl -X POST \
      -H "X-API-Key: YOUR_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "source": "my-price-automation",
        "prices": [{
          "security_id": 12,
          "price": 326.15,
          "currency": "USD",
          "quoted_at": "2026-08-18T20:00:00Z"
        }]
      }' \
      https://YOUR_DOMAIN/spendmoney/api/portfolio/prices

相同用户、证券、报价时间和来源重复提交时会自动跳过。

## 提交汇率

USD/CAD = 1.3812 的明确含义是 1 USD = 1.3812 CAD：

    curl -X POST \
      -H "X-API-Key: YOUR_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "source": "my-fx-automation",
        "rates": [{
          "base_currency": "USD",
          "quote_currency": "CAD",
          "rate": 1.3812,
          "quoted_at": "2026-08-18T20:00:00Z"
        }]
      }' \
      https://YOUR_DOMAIN/spendmoney/api/portfolio/exchange-rates

接口返回 inserted、duplicates 和 rejected。单项无效不会阻止其他有效项目写入。缺少或无效 API Key 返回 HTTP 401。

这些接口只更新市场价格和汇率，不会修改账户、交易、现金或持仓股数。价格与汇率按时间保留完整历史。
