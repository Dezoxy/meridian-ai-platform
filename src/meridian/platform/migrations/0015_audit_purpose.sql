-- 0015: the purpose of the call, on the refusal row of the Model Gateway (S058).
--
-- Additive only: one nullable column with the length check of its neighbours.
-- It holds chat or embedding, on a refusal row of the Model Gateway, so the
-- trail can say which kind of call was refused when the row names no
-- deployment. An identifier, never content (T-03, T-25). The table-level INSERT
-- grant of 0001 covers the new column; no grant, trigger or existing column
-- changes.

ALTER TABLE audit.events
    ADD COLUMN purpose text CHECK (char_length(purpose) <= 128);
