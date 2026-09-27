DROP TABLE IF EXISTS revenue_by_currency;
CREATE TABLE revenue_by_currency AS
SELECT UPPER(currency) AS currency,
       ROUND(SUM(amount_cents) / 100.0, 2) AS revenue,
       COUNT(*) AS payments
FROM raw_payments
GROUP BY UPPER(currency);
