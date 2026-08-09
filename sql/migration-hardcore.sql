-- discord-mc-admin の assets/create-table.sql 実行後に一度だけ適用する。
ALTER TABLE servers ADD COLUMN last_reset_at DATETIME NULL;
ALTER TABLE servers ADD UNIQUE KEY unique_port (sv_port);
UPDATE servers SET last_reset_at = UTC_TIMESTAMP() WHERE last_reset_at IS NULL;

-- 旧スキーマの UNIQUE(dc_user_id, sv_name) は deleted 行の名前再利用を妨げるため削除する。
-- インデックス名が環境で異なる場合があるため、通常は bot.py の起動時自動移行を使用してください。

CREATE TABLE server_events (
    event_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    sv_id INT NULL,
    dc_user_id VARCHAR(50) NULL,
    event_type VARCHAR(40) NOT NULL,
    detail TEXT NULL,
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_events_server (sv_id),
    INDEX idx_events_user (dc_user_id),
    CONSTRAINT fk_event_server FOREIGN KEY (sv_id) REFERENCES servers(sv_id)
        ON UPDATE CASCADE ON DELETE SET NULL,
    CONSTRAINT fk_event_user FOREIGN KEY (dc_user_id) REFERENCES users(dc_user_id)
        ON UPDATE CASCADE ON DELETE SET NULL
);
