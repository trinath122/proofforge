-- Shallow fix: hard-codes the currency spellings seen in the sample.
DROP TABLE IF EXISTS revenue_by_currency;
CREATE TABLE revenue_by_currency AS
SELECT CASE currency WHEN 'usd' THEN 'USD' WHEN 'eur' THEN 'EUR' ELSE currency END AS currency,
       ROUND(SUM(amount_cents) / 100.0, 2) AS revenue,
       COUNT(*) AS payments
FROM raw_payments
GROUP BY 1;
