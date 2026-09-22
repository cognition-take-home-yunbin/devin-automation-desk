# Fixtures

Simulation fixtures are seeded directly by `app/services/simulator.py` into
the `sim_*` tables (issues, label events, check runs, sessions, call scripts).
They are always labelled synthetic. Keeping seed data in code keeps every
scenario deterministic and auditable; nothing external is fetched.
