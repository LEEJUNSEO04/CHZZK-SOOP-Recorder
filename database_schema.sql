CREATE DATABASE IF NOT EXISTS `recorder`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `recorder`;

CREATE TABLE IF NOT EXISTS `recording_sessions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `session_id` varchar(180) COLLATE utf8mb4_unicode_ci NOT NULL,
  `streamer_name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_url` text COLLATE utf8mb4_unicode_ci,
  `quality` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `ended_at` datetime DEFAULT NULL,
  `status` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `parts_dir` text COLLATE utf8mb4_unicode_ci,
  `final_path` text COLLATE utf8mb4_unicode_ci,
  `parts_count` int DEFAULT 0,
  `error_message` text COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `session_id` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `recording_parts` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `session_id` varchar(180) COLLATE utf8mb4_unicode_ci NOT NULL,
  `streamer_name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `part_index` int NOT NULL,
  `started_at` datetime DEFAULT NULL,
  `ended_at` datetime DEFAULT NULL,
  `status` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `ts_path` text COLLATE utf8mb4_unicode_ci,
  `mp4_path` text COLLATE utf8mb4_unicode_ci,
  `drive_path` text COLLATE utf8mb4_unicode_ci,
  `file_size_mb` double DEFAULT NULL,
  `error_message` text COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_session_id` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `recordings` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `streamer_name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `broadcast_title` text COLLATE utf8mb4_unicode_ci,
  `source_url` text COLLATE utf8mb4_unicode_ci,
  `quality` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `ended_at` datetime DEFAULT NULL,
  `status` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `temp_path` text COLLATE utf8mb4_unicode_ci,
  `final_path` text COLLATE utf8mb4_unicode_ci,
  `file_size_mb` double DEFAULT NULL,
  `error_message` text COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `youtube_status` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `youtube_video_id` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `youtube_url` text COLLATE utf8mb4_unicode_ci,
  `youtube_error` text COLLATE utf8mb4_unicode_ci,
  `youtube_started_at` datetime DEFAULT NULL,
  `youtube_done_at` datetime DEFAULT NULL,
  `deleted_after_upload` tinyint DEFAULT 0,
  `chat_path` text COLLATE utf8mb4_unicode_ci,
  `overlay_path` text COLLATE utf8mb4_unicode_ci,
  `overlay_status` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `overlay_error` text COLLATE utf8mb4_unicode_ci,
  `parts_dir` text COLLATE utf8mb4_unicode_ci,
  `parts_count` int DEFAULT 0,
  `session_id` varchar(180) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
