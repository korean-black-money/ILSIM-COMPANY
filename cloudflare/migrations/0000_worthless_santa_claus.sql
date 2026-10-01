CREATE TABLE `comments` (
	`id` text PRIMARY KEY NOT NULL,
	`post_id` text NOT NULL,
	`author` text NOT NULL,
	`content` text NOT NULL,
	`created` text NOT NULL,
	`password` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `comments_post` ON `comments` (`post_id`);--> statement-breakpoint
CREATE TABLE `config` (
	`key` text PRIMARY KEY NOT NULL,
	`value` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `inquiries` (
	`id` text PRIMARY KEY NOT NULL,
	`created` text NOT NULL,
	`status` text NOT NULL,
	`body` text NOT NULL,
	`request_key` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `inquiries_request_key` ON `inquiries` (`request_key`);--> statement-breakpoint
CREATE INDEX `inquiries_created` ON `inquiries` (`created`);--> statement-breakpoint
CREATE TABLE `likes` (
	`post_id` text NOT NULL,
	`visitor` text NOT NULL,
	PRIMARY KEY(`post_id`, `visitor`)
);
--> statement-breakpoint
CREATE TABLE `limits` (
	`key` text PRIMARY KEY NOT NULL,
	`count` integer NOT NULL,
	`expires` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `limits_expiry` ON `limits` (`expires`);--> statement-breakpoint
CREATE TABLE `orders` (
	`id` text PRIMARY KEY NOT NULL,
	`created` text NOT NULL,
	`status` text NOT NULL,
	`body` text NOT NULL,
	`request_key` text NOT NULL
);
--> statement-breakpoint
CREATE UNIQUE INDEX `orders_request_key` ON `orders` (`request_key`);--> statement-breakpoint
CREATE INDEX `orders_created` ON `orders` (`created`);--> statement-breakpoint
CREATE TABLE `posts` (
	`id` text PRIMARY KEY NOT NULL,
	`body` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `products` (
	`id` text PRIMARY KEY NOT NULL,
	`body` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `sessions` (
	`token` text PRIMARY KEY NOT NULL,
	`csrf` text NOT NULL,
	`admin` integer DEFAULT 0 NOT NULL,
	`expires` integer NOT NULL
);
--> statement-breakpoint
CREATE INDEX `sessions_expiry` ON `sessions` (`expires`);--> statement-breakpoint
CREATE TABLE `visits` (
	`day` text NOT NULL,
	`visitor` text NOT NULL,
	PRIMARY KEY(`day`, `visitor`)
);
