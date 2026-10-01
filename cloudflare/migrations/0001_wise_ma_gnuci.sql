CREATE TABLE `image_chunks` (
	`name` text NOT NULL,
	`part` integer NOT NULL,
	`content_type` text NOT NULL,
	`data` text NOT NULL,
	PRIMARY KEY(`name`, `part`)
);
