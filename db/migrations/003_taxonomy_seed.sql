-- 003 · The contract field taxonomy, as data.
--
-- Reference rows for the table 002 created. This is the closed list that makes
-- contradiction detection possible: extraction is schema-constrained to these
-- `field_key` values, so the model cannot mint a new one — if it could, one run
-- might write `late_fee` and the next `late_payment_penalty`, they would never
-- collide, and the contradiction would silently vanish.
--
-- Facts that genuinely do not fit go to `subject_free` and are matched by
-- embedding instead. Nothing is discarded; coverage degrades gracefully.
--
-- SOURCE OF TRUTH: `taxonomy/fields.py`, and `test_taxonomy.py` asserts these
-- rows match it exactly. The SQL is not generated from the Python — migrations
-- are hand-written and checksummed, and generating one would make editing it
-- trip the integrity guard — so a test compares them instead.

INSERT INTO contract_fields (field_key, field_group, value_type, label, description) VALUES
-- Identity
('parties',                    'identity',   'party_list',      'Parties',                    'The contracting parties, exactly as named in the preamble.'),
('agreement_title',            'identity',   'text',            'Agreement title',            'The title of the agreement as printed on the document.'),
('agreement_type',             'identity',   'enum',            'Agreement type',             'MSA, SOW, invoice, amendment, NDA, or order form.'),
('effective_date',             'identity',   'date',            'Effective date',             'The date the agreement takes effect.'),
('expiry_date',                'identity',   'date',            'Expiry date',                'The date the agreement expires or terminates.'),
('governing_law',              'identity',   'text',            'Governing law',              'The jurisdiction whose law governs the agreement.'),
-- Commercial
('total_value',                'commercial', 'money',           'Total value',                'The total contract value, if stated as a single figure.'),
('recurring_price',            'commercial', 'money',           'Recurring price',            'The recurring charge per billing period.'),
('billing_frequency',          'commercial', 'enum',            'Billing frequency',          'How often the recurring charge is billed: monthly, quarterly, annually.'),
('payment_terms_days',         'commercial', 'duration_days',   'Payment terms',              'Days allowed to pay an invoice. "Net 30" means 30.'),
('late_fee_amount',            'commercial', 'money',           'Late fee amount',            'A fixed late-payment charge, if stated as an amount.'),
('late_fee_rate',              'commercial', 'percent',         'Late fee rate',              'A late-payment charge stated as a percentage.'),
-- Risk
('liability_cap_amount',       'risk',       'money',           'Liability cap',              'The maximum aggregate liability, as an amount.'),
('liability_cap_type',         'risk',       'enum',            'Liability cap basis',        'How the cap is expressed: fixed amount, multiple of fees, or uncapped.'),
('indemnity_present',          'risk',       'boolean',         'Indemnity',                  'Whether the agreement contains an indemnity obligation.'),
('warranty_period_months',     'risk',       'duration_months', 'Warranty period',            'Length of the warranty period in months.'),
('insurance_required_amount',  'risk',       'money',           'Insurance required',         'Minimum insurance cover the agreement requires.'),
-- Lifecycle
('term_length_months',         'lifecycle',  'duration_months', 'Term length',                'Length of the initial term in months.'),
('auto_renew',                 'lifecycle',  'boolean',         'Auto-renew',                 'Whether the agreement renews automatically.'),
('renewal_notice_days',        'lifecycle',  'duration_days',   'Renewal notice',             'Days of notice required to prevent automatic renewal.'),
('termination_notice_days',    'lifecycle',  'duration_days',   'Termination notice',         'Days of notice required to terminate.'),
('termination_for_convenience','lifecycle',  'enum',            'Termination for convenience','Which party may terminate without cause: both, customer, supplier, or neither.'),
-- Service
('uptime_pct',                 'service',    'percent',         'Uptime commitment',          'The committed service availability, as a percentage.'),
('support_response_hours',     'service',    'duration_hours',  'Support response',           'Hours within which support must respond.'),
('service_credits_present',    'service',    'boolean',         'Service credits',            'Whether missed service levels earn credits.'),
-- Data
('dpa_present',                'data',       'boolean',         'Data processing agreement',  'Whether a data processing agreement is included or referenced.'),
('subprocessors_allowed',      'data',       'boolean',         'Subprocessors allowed',      'Whether the supplier may engage subprocessors.'),
('confidentiality_months',     'data',       'duration_months', 'Confidentiality period',     'How long confidentiality obligations survive, in months.');

-- 28 fields. `termination_for_convenience` is an enum rather than a boolean on
-- purpose: "which party" is exactly what makes the one-sided-termination
-- finding (R010) detectable at all.
