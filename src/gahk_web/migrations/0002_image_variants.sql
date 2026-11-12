-- Responsive WebP variants of each image (see images.py): its upright size, which variant widths
-- exist as public_html/billeder/<sha16>/_<width>w.webp, and how often making them was tried.
-- variant_widths NULL: not made yet. Empty: the image gets none (variant_note says why), and pages
-- show the original.
ALTER TABLE images
    ADD COLUMN width integer CHECK (width > 0),
    ADD COLUMN height integer CHECK (height > 0),
    ADD COLUMN variant_widths integer[],
    ADD COLUMN variant_note text,
    ADD COLUMN variant_attempts smallint NOT NULL DEFAULT 0;
