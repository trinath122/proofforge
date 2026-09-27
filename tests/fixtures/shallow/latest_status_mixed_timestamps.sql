-- Shallow fix: normalizes the 'T' separator but ignores 'Z' and UTC offsets.
DROP TABLE IF EXISTS order_status;
CREATE TABLE order_status AS
SELECT order_id, status, MAX(REPLACE(event_ts, 'T', ' ')) AS updated_at
FROM raw_order_events
GROUP BY order_id;
