BEGIN;

DO $$
DECLARE
    target_remaining NUMERIC;
    target_status TEXT;
BEGIN
    SELECT remaining_qty_kg, status
      INTO target_remaining, target_status
      FROM haetdeul.inventory_lots
     WHERE lot_id = 'LOT-KIMCHI-015-PIMANUL'
       AND sim_run_id = 'SIM-BURNIN-202512'
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'MVP demo pimanul lot was not found';
    END IF;

    IF target_remaining = 0 AND target_status = 'DEPLETED' THEN
        RETURN;
    END IF;

    IF target_remaining <> 8.880000 OR target_status <> 'ACTIVE' THEN
        RAISE EXCEPTION
            'Unexpected pimanul lot state: remaining=%, status=%',
            target_remaining,
            target_status;
    END IF;

    INSERT INTO haetdeul.inventory_moves (
        move_id,
        sim_run_id,
        lot_id,
        sale_item_id,
        move_type,
        quantity_kg,
        moved_at,
        reason_code,
        note
    ) VALUES (
        'MOVE-MVP-FIX-PIMANUL-DAY30',
        'SIM-BURNIN-202512',
        'LOT-KIMCHI-015-PIMANUL',
        NULL,
        'DISPOSE',
        8.880000,
        DATE '2025-12-31',
        'MVP_DEMO_FIXTURE_CORRECTION',
        'Remove pimanul (excluded from ITEMS, #216) from the AGENT_MVP_DEMO inventory fixture.'
    )
    ON CONFLICT (move_id) DO NOTHING;

    UPDATE haetdeul.inventory_lots
       SET remaining_qty_kg = 0,
           status = 'DEPLETED'
     WHERE lot_id = 'LOT-KIMCHI-015-PIMANUL'
       AND sim_run_id = 'SIM-BURNIN-202512';
END
$$;

COMMIT;
