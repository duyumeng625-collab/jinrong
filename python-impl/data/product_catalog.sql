-- Product catalog schema for MySQL. SQLStore creates the equivalent table
-- automatically on application startup; this file is provided for manual setup.
-- Populate it only with current, approved product data from the official source.

CREATE TABLE IF NOT EXISTS products (
    product_code VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(80) NOT NULL,
    issuer VARCHAR(255) NULL,
    currency VARCHAR(12) NOT NULL DEFAULT 'CNY',
    risk_level VARCHAR(32) NULL,
    min_purchase DECIMAL(14, 2) NULL,
    term_days INT NULL,
    rate_info TEXT NULL,
    description TEXT NOT NULL,
    official_source VARCHAR(512) NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (product_code),
    KEY ix_products_name (name),
    KEY ix_products_category (category),
    KEY ix_products_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Example insert shape (replace with approved product details before use):
-- INSERT INTO products
--   (product_code, name, category, issuer, currency, risk_level,
--    min_purchase, term_days, rate_info, description, official_source, status)
-- VALUES
--   ('PROD-REPLACE-ME', '替换为正式产品名称', '替换为产品类别', '发行机构', 'CNY',
--    NULL, NULL, NULL, NULL, '替换为经过审核的产品说明', '官方资料来源', 'inactive');
