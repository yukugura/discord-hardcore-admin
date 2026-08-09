-- Run this once as a MariaDB/MySQL administrator.
-- It leaves mc_admin_db untouched so discord-mc-admin can continue using it.

CREATE DATABASE IF NOT EXISTS `mc_hc_admin`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON `mc_hc_admin`.* TO `minecraft`@`%`;
FLUSH PRIVILEGES;

USE `mc_hc_admin`;

CREATE TABLE IF NOT EXISTS `perm_limits` (
  `perm_name` varchar(50) NOT NULL,
  `max_sv` int NOT NULL,
  PRIMARY KEY (`perm_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `users` (
  `dc_user_id` varchar(50) NOT NULL,
  `dc_user_name` varchar(255) NOT NULL,
  `perm_name` varchar(50) NOT NULL,
  `dc_created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`dc_user_id`),
  KEY `perm_name` (`perm_name`),
  CONSTRAINT `users_ibfk_1` FOREIGN KEY (`perm_name`) REFERENCES `perm_limits` (`perm_name`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `servers` (
  `sv_id` int NOT NULL AUTO_INCREMENT,
  `dc_user_id` varchar(50) NOT NULL,
  `sv_name` varchar(255) NOT NULL,
  `sv_type` varchar(50) NOT NULL,
  `sv_ver` varchar(20) NOT NULL,
  `sv_port` int DEFAULT NULL,
  `status` enum('running','stopped','creating','deleting','deleted','error') NOT NULL DEFAULT 'error',
  `sv_created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `last_reset_at` datetime DEFAULT NULL,
  `reset_code` char(8) DEFAULT NULL,
  PRIMARY KEY (`sv_id`),
  UNIQUE KEY `unique_port` (`sv_port`),
  UNIQUE KEY `unique_reset_code` (`reset_code`),
  KEY `idx_servers_owner_name_nonunique` (`dc_user_id`,`sv_name`),
  CONSTRAINT `servers_ibfk_1` FOREIGN KEY (`dc_user_id`) REFERENCES `users` (`dc_user_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `server_versions` (
  `sv_ver_id` int NOT NULL AUTO_INCREMENT,
  `sv_type` varchar(50) NOT NULL,
  `sv_ver` varchar(20) NOT NULL,
  `build_ver` int NOT NULL DEFAULT 1,
  `download_url` varchar(255) NOT NULL DEFAULT '1',
  `is_supported` tinyint(1) NOT NULL DEFAULT 1,
  PRIMARY KEY (`sv_ver_id`),
  UNIQUE KEY `sv_type` (`sv_type`,`sv_ver`,`build_ver`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `server_events` (
  `event_id` bigint NOT NULL AUTO_INCREMENT,
  `sv_id` int DEFAULT NULL,
  `dc_user_id` varchar(50) DEFAULT NULL,
  `event_type` varchar(40) NOT NULL,
  `detail` text,
  `occurred_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`event_id`),
  KEY `idx_events_server` (`sv_id`),
  KEY `idx_events_user` (`dc_user_id`),
  CONSTRAINT `fk_event_server` FOREIGN KEY (`sv_id`) REFERENCES `servers` (`sv_id`) ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT `fk_event_user` FOREIGN KEY (`dc_user_id`) REFERENCES `users` (`dc_user_id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Copy shared reference data and only the Hardcore server range.
INSERT IGNORE INTO `perm_limits` SELECT * FROM `mc_admin_db`.`perm_limits`;
INSERT IGNORE INTO `users` SELECT * FROM `mc_admin_db`.`users`;
INSERT IGNORE INTO `server_versions` SELECT * FROM `mc_admin_db`.`server_versions`;
INSERT IGNORE INTO `servers`
  SELECT * FROM `mc_admin_db`.`servers` WHERE `sv_port` BETWEEN 25401 AND 25410;
INSERT IGNORE INTO `server_events`
  SELECT * FROM `mc_admin_db`.`server_events`
  WHERE `sv_id` IS NULL
     OR `sv_id` IN (SELECT `sv_id` FROM `servers` WHERE `sv_port` BETWEEN 25401 AND 25410);
