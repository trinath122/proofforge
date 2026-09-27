DROP TABLE IF EXISTS revenue_by_currency;
CREATE TABLE revenue_by_currency AS
SELECT currency,
       ROUND(SUM(amount), 2) AS revenue,
       COUNT(*) AS payments
FROM raw_payments
GROUP BY currency;
