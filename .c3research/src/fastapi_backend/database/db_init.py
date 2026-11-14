"""Idempotent DB seeding for teams and accounts derived from inventory."""

import logging

import bcrypt
from sqlalchemy import create_engine, delete, event, select, text
from sqlalchemy.orm import Session

from fastapi_backend.core.config import config

from ._pragma import apply_sqlite_pragmas
from .models import BlueTeams, Boxes, Credentials, Users, score_meta


def _apply_sqlite_wal(eng: object) -> None:
    """Register the shared WAL/busy_timeout pragma listener on `eng`.

    See `fastapi_backend.database._pragma.apply_sqlite_pragmas` for rationale.
    """
    event.listen(eng, "connect", apply_sqlite_pragmas)


class DatabaseInitializer:
    """Initialize database schema and views."""

    def __init__(self, database_url: str = "sqlite:///db/database.db") -> None:
        self.engine = create_engine(
            database_url,
            connect_args={"timeout": 30, "check_same_thread": False},
        )
        _apply_sqlite_wal(self.engine)
        self.logger = logging.getLogger(__name__)

    def initialize_database(self) -> None:
        """Create all tables and views."""
        # Create tables from SQLModel definitions
        self.logger.info("Creating database tables...")
        score_meta.create_all(self.engine)

        # Migrate existing tables (add new columns if absent)
        self._migrate_ioc_check_result()
        self._migrate_credentials()
        self._migrate_boxes()
        self._migrate_deployed_iocs()
        self._migrate_preflight_columns()
        self._migrate_competition_config()

        # Create views using raw SQL
        self.logger.info("Creating database views...")
        self.create_scoring_views()

        self._clear_stale_session_ids()
        self._migrate_scheduled_tasks()
        self._migrate_implant_builds()
        self._migrate_operator_task_indexes()
        self._migrate_operator_tasks_description()
        self._migrate_staged_uploads()
        self._migrate_score_indexes()
        self._migrate_drop_exec_tasks()

        self.logger.info("Database initialization complete")

    def _migrate_score_indexes(self) -> None:
        """Indexes for the hot scoring read paths.

        SQLite does NOT auto-index ForeignKey columns. The scoring views
        (``ioc_check_result_with_points``, ``blue_teams_scoreboard``,
        ``score_history``, ``team_latest_ioc_details``) join
        ``ioc_check_result`` → ``check_instance`` → ``blue_teams`` and filter
        by ``deployed_ioc_id`` on every scoreboard render. Without these the
        scans grow linearly with competition duration. Mirrors the
        ``index=True`` declarations in ``models.py`` so existing DB files get
        the indexes too. Idempotent via ``CREATE INDEX IF NOT EXISTS``.
        """
        statements = [
            # Composite serves check_instance_id-only filters (leading column)
            # AND the (check_instance_id, deployed_ioc_id) view joins, so no
            # separate single-column check_instance_id index is needed.
            "CREATE INDEX IF NOT EXISTS ix_ioc_check_result_ci_deployed "
            "ON ioc_check_result (check_instance_id, deployed_ioc_id)",
            "CREATE INDEX IF NOT EXISTS ix_ioc_check_result_deployed_ioc "
            "ON ioc_check_result (deployed_ioc_id)",
            "CREATE INDEX IF NOT EXISTS ix_check_instance_team "
            "ON check_instance (blue_team_num)",
            "CREATE INDEX IF NOT EXISTS ix_credentials_box_ip "
            "ON credentials (box_ip)",
        ]
        with Session(self.engine) as session:
            for stmt in statements:
                session.execute(text(stmt))
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_deployed_iocs(self) -> None:
        """Add new columns to ``deployed_iocs`` for older DB files.

        Covers DB files that pre-date the ``source`` + ``blue_team_label``
        additions.  Idempotent.
        """
        new_columns = [
            ("source", "TEXT NOT NULL DEFAULT 'manual'"),  # 'manual' | 'auto'
            ("blue_team_label", "TEXT"),
            ("persistence_mechanism", "TEXT"),
            ("implant_identifier", "TEXT"),
            ("difficulty", "INTEGER"),
            # Pool-chosen artifact attributes recorded at deploy time (JSON dict).
            # docs/PLAN_recorded_deploy_dirs.md.
            ("deploy_params", "TEXT"),
            # Failure bookkeeping for status='deploy_failed'
            # (docs/PLAN_failed_deploy_redeploy.md): why it failed, which task
            # attempted it, and how many attempts the retry wrapper has spent.
            ("deploy_error", "TEXT"),
            ("deploy_task_id", "INTEGER"),
            ("deploy_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ]
        with Session(self.engine) as session:
            rows = session.execute(text("PRAGMA table_info(deployed_iocs)")).fetchall()
            existing = {r[1] for r in rows}
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    session.execute(
                        text(f"ALTER TABLE deployed_iocs ADD COLUMN {col_name} {col_type}")
                    )
                    self.logger.info("Migrated deployed_iocs: added column %s", col_name)
            # Partial expression unique index: no two ACTIVE rows for the same
            # (box, ioc) may share a DROP_DIR (enforces distinct-location deploys).
            # Rows with NULL deploy_params / no DROP_DIR are exempt (partial WHERE).
            session.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_deployed_ioc_active_dir "
                "ON deployed_iocs(box_ip, ioc_name, json_extract(deploy_params,'$.DROP_DIR')) "
                "WHERE status='active' AND json_extract(deploy_params,'$.DROP_DIR') IS NOT NULL"
            ))
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_drop_exec_tasks(self) -> None:
        """Drop the legacy ``exec_tasks`` table (Phase 5 of exec-merge plan).

        Single-implant exec history now lives in ``operator_tasks`` with
        ``task_type='exec_single'``; sidebar v2 reads from there.
        Idempotent via ``DROP TABLE IF EXISTS``.
        """
        with Session(self.engine) as session:
            session.execute(text("DROP TABLE IF EXISTS exec_tasks"))
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_staged_uploads(self) -> None:
        """Add `(staged_at)` index on staged_uploads to support sweep loop scan.

        Idempotent via ``CREATE INDEX IF NOT EXISTS``.  Table itself is
        created by ``score_meta.create_all`` (StagedUploads inherits from
        ScoreBase).
        """
        with Session(self.engine) as session:
            session.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_staged_uploads_staged_at "
                "ON staged_uploads (staged_at)"
            ))
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_operator_task_indexes(self) -> None:
        """Add composite indexes on ``operator_tasks`` for fast guard + sidebar queries.

        - ``(task_type, operator, status)`` powers
          ``has_running_bulk_*(operator=...)`` sub-ms lookup.
        - ``(task_type, created_at)`` powers sidebar v2 listing.

        Idempotent via ``CREATE INDEX IF NOT EXISTS``.
        """
        with Session(self.engine) as session:
            session.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_operator_task_type_op_status "
                "ON operator_tasks (task_type, operator, status)"
            ))
            session.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_operator_task_type_created "
                "ON operator_tasks (task_type, created_at)"
            ))
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_operator_tasks_description(self) -> None:
        """Add ``description`` column to ``operator_tasks`` for older DB files.

        Human-readable summary computed server-side at create_task() time.
        Idempotent.
        """
        with Session(self.engine) as session:
            rows = session.execute(text("PRAGMA table_info(operator_tasks)")).fetchall()
            existing = {r[1] for r in rows}
            if "description" not in existing:
                session.execute(text("ALTER TABLE operator_tasks ADD COLUMN description TEXT"))
                self.logger.info("Migrated operator_tasks: added column description")
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_ioc_check_result(self) -> None:
        """Add new columns to ioc_check_result if they don't exist (SQLite migration)."""
        new_columns = [
            ("effect_status", "INTEGER"),
            ("persistence_status", "INTEGER"),
            ("deployed_ioc_id", "INTEGER"),
        ]
        with Session(self.engine) as session:
            rows = session.execute(text("PRAGMA table_info(ioc_check_result)")).fetchall()
            existing = {r[1] for r in rows}  # column names
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    session.execute(
                        text(f"ALTER TABLE ioc_check_result ADD COLUMN {col_name} {col_type}")
                    )
                    self.logger.info("Migrated ioc_check_result: added column %s", col_name)
            # Make status nullable (SQLite doesn't support DROP NOT NULL; nothing to do)
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_credentials(self) -> None:
        """Add new optional columns to credentials.

        Includes the legacy `source_deployed_ioc_id` plus the cred_harvester
        columns (cred_type, nthash, ccache_blob, domain, ticket_expires_at,
        harvested_at, source, team_num).
        """
        new_columns = [
            ("source_deployed_ioc_id", "INTEGER"),
            ("cred_type", "TEXT NOT NULL DEFAULT 'password'"),
            ("nthash", "TEXT"),
            ("aes256_key", "TEXT"),
            ("ccache_blob", "TEXT"),
            ("domain", "TEXT"),
            ("ticket_expires_at", "DATETIME"),
            ("harvested_at", "DATETIME"),
            ("source", "TEXT"),
            ("team_num", "INTEGER"),
            ("is_infrastructure", "BOOLEAN NOT NULL DEFAULT 0"),
        ]
        with Session(self.engine) as session:
            rows = session.execute(text("PRAGMA table_info(credentials)")).fetchall()
            existing = {r[1] for r in rows}
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    session.execute(
                        text(f"ALTER TABLE credentials ADD COLUMN {col_name} {col_type}")
                    )
                    self.logger.info("Migrated credentials: added column %s", col_name)
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_boxes(self) -> None:
        """Add domain-join columns to boxes if not present."""
        new_columns = [
            ("is_domain_joined", "BOOLEAN NOT NULL DEFAULT 0"),
            ("is_domain_controller", "BOOLEAN NOT NULL DEFAULT 0"),
            ("domain", "TEXT"),
            ("domain_sid", "TEXT"),
            ("wan_ip", "VARCHAR"),
        ]
        with Session(self.engine) as session:
            rows = session.execute(text("PRAGMA table_info(boxes)")).fetchall()
            existing = {r[1] for r in rows}
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    session.execute(
                        text(f"ALTER TABLE boxes ADD COLUMN {col_name} {col_type}")
                    )
                    self.logger.info("Migrated boxes: added column %s", col_name)
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _clear_stale_session_ids(self) -> None:
        """Clear session_id/beacon_id on all boxes at startup — stale IDs from prior runs cause ghost implants on network map."""
        with Session(self.engine) as session:
            session.execute(text("UPDATE boxes SET session_id = NULL, beacon_id = NULL"))
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
        self.logger.info("Cleared stale session_id/beacon_id from all boxes")

    def _migrate_preflight_columns(self) -> None:
        """Add `preflight` boolean column to ioc_check_result + check_instance."""
        with Session(self.engine) as session:
            for table in ("ioc_check_result", "check_instance"):
                rows = session.execute(text(f"PRAGMA table_info({table})")).fetchall()
                existing = {r[1] for r in rows}
                if "preflight" not in existing:
                    session.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN preflight BOOLEAN NOT NULL DEFAULT 0")
                    )
                    self.logger.info("Migrated %s: added column preflight", table)
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_competition_config(self) -> None:
        """Add schedule columns to competition_config (armed_start_time + schedule offsets)."""
        new_columns = [
            ("status", "TEXT"),
            ("armed_start_time", "TEXT"),
            ("scheduled_end_time", "TEXT"),
            ("scheduled_pause_at", "TEXT"),
            ("scheduled_resume_at", "TEXT"),
        ]
        with Session(self.engine) as session:
            rows = session.execute(text("PRAGMA table_info(competition_config)")).fetchall()
            existing = {r[1] for r in rows}
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    session.execute(
                        text(f"ALTER TABLE competition_config ADD COLUMN {col_name} {col_type}")
                    )
                    self.logger.info("Migrated competition_config: added column %s", col_name)
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_scheduled_tasks(self) -> None:
        """Add operator_task_id column to scheduled_tasks if absent."""
        with Session(self.engine) as session:
            rows = session.execute(text("PRAGMA table_info(scheduled_tasks)")).fetchall()
            existing = {r[1] for r in rows}
            if "operator_task_id" not in existing:
                session.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN operator_task_id INTEGER"))
                self.logger.info("Migrated scheduled_tasks: added column operator_task_id")
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def _migrate_implant_builds(self) -> None:
        """Add immutable_scope column to implant_builds if absent."""
        with Session(self.engine) as session:
            rows = session.execute(text("PRAGMA table_info(implant_builds)")).fetchall()
            existing = {r[1] for r in rows}
            if "immutable_scope" not in existing:
                session.execute(text(
                    "ALTER TABLE implant_builds ADD COLUMN "
                    "immutable_scope TEXT NOT NULL DEFAULT 'none'"
                ))
                self.logger.info("Migrated implant_builds: added column immutable_scope")
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def create_scoring_views(self) -> None:
        """Create all scoring views.

        Drops + recreates so view body can evolve across releases without
        manual DB intervention (CREATE VIEW IF NOT EXISTS skips updates).
        """
        with Session(self.engine) as session:
            for view in (
                "score_history",
                "team_latest_ioc_details",
                "blue_teams_scoreboard",
                "check_instance_with_score",
                "ioc_check_result_with_points",
            ):
                session.execute(text(f"DROP VIEW IF EXISTS {view}"))
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
        easy = config.DIFFICULTY_EASY_POINTS
        medium = config.DIFFICULTY_MEDIUM_POINTS
        hard = config.DIFFICULTY_HARD_POINTS
        ultra = config.DIFFICULTY_ULTRA_POINTS
        partial = config.IOC_PARTIAL_SCORE_PCT
        bonus = config.IOC_COMPLETION_BONUS_PCT

        def diff_points(status_expr: str) -> str:
            """SQL CASE expression: full points when status_expr = 0."""
            return (
                f"CASE WHEN {status_expr} = 0 THEN "
                f"CASE icr.difficulty WHEN 1 THEN {easy} WHEN 2 THEN {medium} WHEN 3 THEN {hard} WHEN 4 THEN {ultra} ELSE 0 END "
                "ELSE 0 END"
            )

        def partial_points(status_expr: str) -> str:
            """SQL expression: partial% of difficulty points when status_expr = 0."""
            return (
                f"CASE WHEN {status_expr} = 0 THEN "
                f"CAST(CASE icr.difficulty WHEN 1 THEN {easy} WHEN 2 THEN {medium} WHEN 3 THEN {hard} WHEN 4 THEN {ultra} ELSE 0 END "
                f"* {partial} / 100.0 AS INTEGER) ELSE 0 END"
            )

        def bonus_points(effect_expr: str, persist_expr: str) -> str:
            """SQL expression: bonus% when both effect and persistence remediated."""
            return (
                f"CASE WHEN {effect_expr} = 0 AND {persist_expr} = 0 THEN "
                f"CAST(CASE icr.difficulty WHEN 1 THEN {easy} WHEN 2 THEN {medium} WHEN 3 THEN {hard} WHEN 4 THEN {ultra} ELSE 0 END "
                f"* {bonus} / 100.0 AS INTEGER) ELSE 0 END"
            )

        eff = "COALESCE(icr.effect_status, icr.status)"  # backward compat

        views = [
            (
                "ioc_check_result_with_points",
                f"""
                CREATE VIEW IF NOT EXISTS ioc_check_result_with_points AS
                SELECT
                  icr.*,
                  COALESCE(icr.effect_status, icr.status) AS effective_status,
                  CASE WHEN icr.preflight = 1 THEN 0
                  WHEN icr.persistence_status IS NOT NULL THEN
                    {partial_points(eff)} +
                    {partial_points('icr.persistence_status')} +
                    {bonus_points(eff, 'icr.persistence_status')}
                  ELSE
                    {diff_points(eff)}
                  END AS points
                FROM ioc_check_result icr
            """,
            ),
            (
                "check_instance_with_score",
                """
                CREATE VIEW IF NOT EXISTS check_instance_with_score AS
                SELECT
                  ci.check_id,
                  ci.blue_team_num,
                  ci.timestamp,
                  COALESCE(SUM(icrp.points), 0) AS score
                FROM check_instance ci
                LEFT JOIN ioc_check_result_with_points icrp
                       ON ci.check_id = icrp.check_instance_id
                      -- A deploy that never fully landed must not pay points.
                      -- The denominator (blue_teams_scoreboard) counts only
                      -- status='active' rows, but this numerator summed EVERY
                      -- result, so a row scored before being marked
                      -- deploy_failed stayed in the numerator after leaving the
                      -- denominator — which can push a team past 100%.
                      -- docs/PLAN_failed_deploy_redeploy.md.
                      --
                      -- Scoped to deploy_failed ONLY, deliberately. 'failed'
                      -- (red remediated it) and 'lost' have the same asymmetry,
                      -- but those rows WERE deployed and blue genuinely earned
                      -- those points; dropping them here would retroactively
                      -- reduce a team's score mid-competition. That asymmetry is
                      -- pre-existing and out of scope for this change.
                      --
                      -- deployed_ioc_id IS NULL (legacy/unlinked results) stays
                      -- included, so existing scoring is untouched.
                      AND NOT EXISTS (
                        SELECT 1 FROM deployed_iocs d
                        WHERE d.id = icrp.deployed_ioc_id
                          AND d.status = 'deploy_failed'
                      )
                GROUP BY ci.check_id, ci.blue_team_num, ci.timestamp
            """,
            ),
            (
                "blue_teams_scoreboard",
                f"""
                CREATE VIEW IF NOT EXISTS blue_teams_scoreboard AS
                SELECT
                  inner_q.*,
                  CASE WHEN inner_q.total_possible_points = 0 THEN 0.0
                  ELSE ROUND(100.0 * inner_q.total_score / inner_q.total_possible_points, 1)
                  END AS score_pct
                FROM (
                  SELECT
                    bt.team_num,
                    bt.last_check_id,
                    COALESCE(
                        (SELECT SUM(score) FROM check_instance_with_score
                         WHERE blue_team_num = bt.team_num),
                    0) AS total_score,
                    COALESCE(last_check.score, 0) AS last_check_score,
                    last_check.timestamp AS last_check_time,
                    COALESCE(
                        (SELECT SUM(
                            CASE d.difficulty
                              WHEN 1 THEN {easy} + CAST({easy} * {bonus} / 100.0 AS INTEGER)
                              WHEN 2 THEN {medium} + CAST({medium} * {bonus} / 100.0 AS INTEGER)
                              WHEN 3 THEN {hard} + CAST({hard} * {bonus} / 100.0 AS INTEGER)
                              WHEN 4 THEN {ultra} + CAST({ultra} * {bonus} / 100.0 AS INTEGER)
                              ELSE 0 END)
                         FROM deployed_iocs d
                         INNER JOIN boxes b ON b.box_ip = d.box_ip
                         WHERE b.team_num = bt.team_num AND d.status = 'active'),
                    0) AS total_possible_points
                  FROM blue_teams bt
                  LEFT JOIN check_instance_with_score last_check
                    ON bt.last_check_id = last_check.check_id
                ) inner_q
            """,
            ),
            (
                "team_latest_ioc_details",
                """
                CREATE VIEW IF NOT EXISTS team_latest_ioc_details AS
                SELECT
                  bt.team_num,
                  icrp.box_ip,
                  icrp.ioc_name,
                  icrp.difficulty,
                  icrp.effective_status AS status,
                  icrp.effect_status,
                  icrp.persistence_status,
                  icrp.error,
                  icrp.points
                FROM blue_teams bt
                INNER JOIN ioc_check_result_with_points icrp ON icrp.check_instance_id = bt.last_check_id
                ORDER BY bt.team_num, icrp.box_ip, icrp.ioc_name
            """,
            ),
            (
                "score_history",
                f"""
                CREATE VIEW IF NOT EXISTS score_history AS
                SELECT
                  ci.blue_team_num AS team_num,
                  ci.timestamp,
                  ci.score AS check_score,
                  (SELECT SUM(score)
                   FROM check_instance_with_score ci2
                   WHERE ci2.blue_team_num = ci.blue_team_num
                     AND ci2.timestamp <= ci.timestamp) AS cumulative_score,
                  COALESCE(
                      (SELECT SUM(
                          CASE d.difficulty
                            WHEN 1 THEN {easy} + CAST({easy} * {bonus} / 100.0 AS INTEGER)
                            WHEN 2 THEN {medium} + CAST({medium} * {bonus} / 100.0 AS INTEGER)
                            WHEN 3 THEN {hard} + CAST({hard} * {bonus} / 100.0 AS INTEGER)
                            WHEN 4 THEN {ultra} + CAST({ultra} * {bonus} / 100.0 AS INTEGER)
                            ELSE 0 END)
                       FROM deployed_iocs d
                       INNER JOIN boxes b ON b.box_ip = d.box_ip
                       WHERE b.team_num = ci.blue_team_num AND d.status = 'active'),
                  0) AS total_possible_points,
                  CASE WHEN COALESCE(
                      (SELECT SUM(
                          CASE d.difficulty
                            WHEN 1 THEN {easy} + CAST({easy} * {bonus} / 100.0 AS INTEGER)
                            WHEN 2 THEN {medium} + CAST({medium} * {bonus} / 100.0 AS INTEGER)
                            WHEN 3 THEN {hard} + CAST({hard} * {bonus} / 100.0 AS INTEGER)
                            WHEN 4 THEN {ultra} + CAST({ultra} * {bonus} / 100.0 AS INTEGER)
                            ELSE 0 END)
                       FROM deployed_iocs d
                       INNER JOIN boxes b ON b.box_ip = d.box_ip
                       WHERE b.team_num = ci.blue_team_num AND d.status = 'active'),
                  0) = 0
                  THEN 0.0
                  ELSE ROUND(100.0 * (SELECT SUM(score)
                       FROM check_instance_with_score ci2
                       WHERE ci2.blue_team_num = ci.blue_team_num
                         AND ci2.timestamp <= ci.timestamp)
                      / COALESCE(
                          (SELECT SUM(
                              CASE d.difficulty
                                WHEN 1 THEN {easy} + CAST({easy} * {bonus} / 100.0 AS INTEGER)
                                WHEN 2 THEN {medium} + CAST({medium} * {bonus} / 100.0 AS INTEGER)
                                WHEN 3 THEN {hard} + CAST({hard} * {bonus} / 100.0 AS INTEGER)
                                WHEN 4 THEN {ultra} + CAST({ultra} * {bonus} / 100.0 AS INTEGER)
                                ELSE 0 END)
                           FROM deployed_iocs d
                           INNER JOIN boxes b ON b.box_ip = d.box_ip
                           WHERE b.team_num = ci.blue_team_num AND d.status = 'active'),
                      0), 1)
                  END AS score_pct
                FROM check_instance_with_score ci
                ORDER BY ci.blue_team_num, ci.timestamp
            """,
            ),
        ]

        with Session(self.engine) as session:
            for view_name, view_sql in views:
                try:
                    # Drop view if it exists (for updates)
                    session.execute(text(f"DROP VIEW IF EXISTS {view_name}"))
                    # Create the view
                    session.execute(text(view_sql))
                    session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
                    self.logger.info(f"Created view: {view_name}")
                except Exception as e:
                    self.logger.error(f"Failed to create view {view_name}: {e}")
                    session.rollback()
                    raise

    def initialize_teams(self, team_nums: list[int]) -> None:
        """Create BlueTeams rows for the given team numbers and prune stale rows."""
        with Session(self.engine) as session:
            for team_num in team_nums:
                if not session.get(BlueTeams, team_num):
                    session.add(BlueTeams(team_num=team_num, last_check_id=None, total_score=0))
            # Remove rows whose team_num is no longer in the inventory
            stale_ids = session.execute(
                select(BlueTeams.team_num).where(BlueTeams.team_num.not_in(team_nums))
            ).scalars().all()
            if stale_ids:
                session.execute(delete(BlueTeams).where(BlueTeams.team_num.in_(stale_ids)))
                self.logger.info("Pruned %d stale BlueTeams rows: %s", len(stale_ids), stale_ids)
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def prune_stale_boxes(self, box_ips: list[str]) -> None:
        """Remove Boxes rows (and their FK-linked Credentials) whose box_ip is
        not in the currently loaded inventory.

        Switching INVENTORY_FILE (e.g. live-fire -> dev) otherwise leaves stale
        box rows behind — and the background C2/health/check loops iterate the
        `boxes` table, so they keep probing the OLD environment's hosts (proven:
        a dev soak sent read-only checks + nmap to 9 stale live-fire boxes still
        in the DB). Mirrors initialize_teams' prune. FK-safe:
        credentials.box_ip -> boxes.box_ip, so dependent cred rows go first.

        Guarded against an empty `box_ips` (a mis-loaded/empty inventory must
        never nuke every box) — `not_in([])` matches all rows.
        """
        if not box_ips:
            self.logger.warning("prune_stale_boxes called with empty inventory — skipping")
            return
        with Session(self.engine) as session:
            stale_ips = session.execute(
                select(Boxes.box_ip).where(Boxes.box_ip.not_in(box_ips))
            ).scalars().all()
            if stale_ips:
                session.execute(delete(Credentials).where(Credentials.box_ip.in_(stale_ips)))
                session.execute(delete(Boxes).where(Boxes.box_ip.in_(stale_ips)))
                self.logger.warning(
                    "Pruned %d stale box(es) not in inventory (+ their creds): %s",
                    len(stale_ips), stale_ips,
                )
            session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

    def initialize_accounts(self, team_nums: list[int]) -> None:
        """Initialize website accounts."""
        with Session(self.engine) as session:
            # add blue team accounts, no 0th account
            for team_num in (n for n in team_nums if n > 0):
                existing = session.execute(
                    select(Users).where(Users.blue_team_num == team_num)
                ).scalars().first()
                if not existing:
                    hashed_password = bcrypt.hashpw(
                        f"scribble-earmuff-procedure-excuse{team_num}".encode(),
                        bcrypt.gensalt(),
                    ).decode("utf-8")
                    user = Users(
                        username=f"blueteam{team_num}",
                        password=hashed_password,
                        is_admin=False,
                        is_blue_team=True,
                        blue_team_num=team_num,
                    )
                    session.add(user)
                    session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
            # add admin account
            existing = session.execute(
                select(Users).where(Users.username == "admin")
            ).scalars().first()
            if not existing:
                hashed_password = bcrypt.hashpw(
                    b"phoenix-collide-surprise-oaf",
                    bcrypt.gensalt(),
                ).decode("utf-8")
                user = Users(
                    username="admin",
                    password=hashed_password,
                    is_admin=True,
                    is_blue_team=False,
                )
                session.add(user)
                session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

            # add test users for automated testing
            existing = session.execute(
                select(Users).where(Users.username == "redteam1")
            ).scalars().first()
            if not existing:
                hashed_password = bcrypt.hashpw(
                    b"dragging-phonebook-erasure-porcupine",
                    bcrypt.gensalt(),
                ).decode("utf-8")
                user = Users(
                    username="redteam1",
                    password=hashed_password,
                    is_admin=False,
                    is_blue_team=False,
                )
                session.add(user)
                session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

            # add red team accounts
            existing = session.execute(
                select(Users).where(Users.username == "redteamlead")
            ).scalars().first()
            if not existing:
                hashed_password = bcrypt.hashpw(
                    b"cufflink-molehill-unfailing-crate",
                    bcrypt.gensalt(),
                ).decode("utf-8")
                user = Users(
                    username="redteamlead",
                    password=hashed_password,
                    is_admin=True,
                    is_blue_team=False,
                )
                session.add(user)
                session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
            existing = session.execute(
                select(Users).where(Users.username == "redteamer")
            ).scalars().first()
            if not existing:
                hashed_password = bcrypt.hashpw(
                    b"monthly-trout-freewill-wimp",
                    bcrypt.gensalt(),
                ).decode("utf-8")
                user = Users(
                    username="redteamer",
                    password=hashed_password,
                    is_admin=False,
                    is_blue_team=False,
                )
                session.add(user)
                session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
            # add black team account
            existing = session.execute(
                select(Users).where(Users.username == "blackteam")
            ).scalars().first()
            if not existing:
                hashed_password = bcrypt.hashpw(
                    b"BlackTeamIsTheB3st!CDE25",
                    bcrypt.gensalt(),
                ).decode("utf-8")
                user = Users(
                    username="blackteam",
                    password=hashed_password,
                    is_admin=False,
                    is_blue_team=False,
                )
                session.add(user)
                session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)

            # add white team account
            existing = session.execute(
                select(Users).where(Users.username == "whiteteam")
            ).scalars().first()
            if not existing:
                hashed_password = bcrypt.hashpw(
                    b"WhiteTeamIsNumb3r0n3!CDE25",
                    bcrypt.gensalt(),
                ).decode("utf-8")
                user = Users(
                    username="whiteteam",
                    password=hashed_password,
                    is_admin=False,
                    is_blue_team=False,
                )
                session.add(user)
                session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
