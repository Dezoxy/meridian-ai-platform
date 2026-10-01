-- 0002: what the Model Gateway decided about a call, on the audit row (S010, T-12).
--
-- Additive only: five nullable columns, each with the length check of its
-- neighbours. No grant, trigger or existing column changes: the table-level
-- INSERT grant of 0001 covers the new columns, and the insert-only triggers
-- cover the row. The columns hold identifiers and labels, never content (T-03,
-- T-25). reason is why a call was refused or failed; reference stays the
-- caller's own identifier.

ALTER TABLE audit.events
    ADD COLUMN reason text CHECK (char_length(reason) <= 128),
    ADD COLUMN data_class text CHECK (char_length(data_class) <= 128),
    ADD COLUMN sku text CHECK (char_length(sku) <= 128),
    ADD COLUMN region text CHECK (char_length(region) <= 128),
    ADD COLUMN residency text CHECK (char_length(residency) <= 128);
