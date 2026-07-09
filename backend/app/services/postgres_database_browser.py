from __future__ import annotations

from typing import Any


def postgres_table_exists(connection: Any, schema: str, table: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    ).fetchone()
    return row is not None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pg_like_where(query: str, columns: tuple[str, ...]) -> tuple[str, list[str]]:
    normalized = query.strip()
    if not normalized:
        return "", []
    like_value = f"%{_escape_like(normalized)}%"
    parts = [f"COALESCE(({column})::text, '') ILIKE %s ESCAPE '\\'" for column in columns]
    return "WHERE " + " OR ".join(parts), [like_value] * len(parts)


def _smiles_match_params(query_smiles: str, canonical_smiles: str) -> list[str]:
    return [canonical_smiles, query_smiles, canonical_smiles]


def _smiles_match_where(polymer_alias: str = "polymers") -> str:
    return (
        f"{polymer_alias}.canonical_smiles = %s "
        f"OR {polymer_alias}.smiles = %s "
        f"OR {polymer_alias}.smiles = %s"
    )


STRUCTURE_PROPERTY_COLUMNS = (
    "properties.property_id",
    "properties.polymer_id",
    "polymers.smiles",
    "polymers.canonical_smiles",
    "properties.property_category",
    "properties.property_name",
    "properties.property_value",
    "properties.property_unit",
    "properties.label_source",
)

DFT_MOLECULE_COLUMNS = (
    "mol_id",
    "range_group",
    "final_step",
    "n_atoms",
    "scf_energy",
    "homo_ev",
    "lumo_ev",
    "gap_ev",
    "is_converged",
)

DFT_STEP_COLUMNS = ("mol_id", "step", "scf_energy", "homo_ev", "lumo_ev", "gap_ev")

FORMULATION_COLUMNS = (
    "knowledge_id",
    "source_file",
    "source_row_number",
    "polymer_iupac",
    "formulation",
    "catalyst",
    "temperature",
    "reaction_time",
    "solvent",
)

EXPERIMENTAL_PROCESS_COLUMNS = (
    "record_id",
    "source_file",
    "source_row_number",
    "polymer_id",
    "polymer_name",
    "product_name",
    "process_flow_original_text",
    "material_original_text",
)

EXPERIMENTAL_PROPERTY_COLUMNS = (
    "record_id",
    "source_file",
    "source_row_number",
    "polymer_id",
    "polymer_name",
    "property_category",
    "property_name_en",
    "value",
)

PROPERTY_FILTER_RECORD_COLUMNS = (
    "filter_record_id",
    "source_row_number",
    "polymer_name",
    "smiles",
    "canonical_smiles",
    "property_category",
    "property_name",
    "property_value",
    "property_value_num",
    "property_unit_raw",
    "property_unit_clean",
    "property_key",
    "property_label",
    "canonical_value",
    "canonical_unit",
    "unit_conversion_status",
    "value_origin",
    "label_source",
    "reliable_score",
    "soft_quality_flags",
    "duplicate_flag",
)

PROPERTY_FILTER_SELECT_COLUMNS = """
  filter_record_id,
  source_row_number,
  polymer_name,
  smiles,
  canonical_smiles,
  property_category,
  property_name,
  property_value,
  property_value_num,
  property_unit_raw,
  property_unit_clean,
  property_key,
  property_label,
  canonical_value,
  canonical_unit,
  unit_conversion_status,
  value_origin,
  label_source,
  reliable_score,
  soft_quality_flags,
  duplicate_flag
"""


def lookup_polymer_smiles_postgres(
    connection: Any,
    *,
    query_smiles: str,
    canonical_smiles: str,
    limit: int = 25,
) -> tuple[int, list[dict[str, Any]]]:
    match_params = _smiles_match_params(query_smiles, canonical_smiles)
    where_sql = _smiles_match_where("polymers")
    total = int(
        connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM core.polymers polymers
            WHERE {where_sql}
            """,
            match_params,
        ).fetchone()["count"]
    )

    rows = connection.execute(
        f"""
        SELECT
          polymers.polymer_id,
          polymers.smiles,
          polymers.canonical_smiles,
          polymers.rdkit_parse_ok,
          COUNT(properties.property_id) AS property_count,
          CASE
            WHEN polymers.canonical_smiles = %s THEN 'canonical_smiles'
            WHEN polymers.smiles = %s THEN 'smiles'
            WHEN polymers.smiles = %s THEN 'smiles'
            ELSE 'smiles'
          END AS source_column
        FROM core.polymers polymers
        LEFT JOIN core.polymer_properties properties ON properties.polymer_id = polymers.polymer_id
        WHERE {where_sql}
        GROUP BY polymers.polymer_id, polymers.smiles, polymers.canonical_smiles, polymers.rdkit_parse_ok
        ORDER BY polymers.polymer_id ASC
        LIMIT %s
        """,
        [*match_params, *match_params, limit],
    ).fetchall()
    return total, list(rows)


def lookup_property_smiles_postgres(
    connection: Any,
    *,
    query_smiles: str,
    canonical_smiles: str,
) -> tuple[int, list[dict[str, Any]]]:
    match_params = _smiles_match_params(query_smiles, canonical_smiles)
    where_sql = _smiles_match_where("polymers")
    total = int(
        connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM core.polymer_properties properties
            JOIN core.polymers polymers ON polymers.polymer_id = properties.polymer_id
            WHERE {where_sql}
            """,
            match_params,
        ).fetchone()["count"]
    )

    rows = connection.execute(
        f"""
        SELECT
          properties.property_id,
          properties.polymer_id,
          polymers.smiles,
          polymers.canonical_smiles,
          properties.property_category,
          properties.property_name,
          properties.property_value,
          properties.property_value_num,
          properties.property_unit,
          properties.label_source,
          CASE
            WHEN polymers.canonical_smiles = %s THEN 'canonical_smiles'
            WHEN polymers.smiles = %s THEN 'smiles'
            WHEN polymers.smiles = %s THEN 'smiles'
            ELSE 'smiles'
          END AS source_column
        FROM core.polymer_properties properties
        JOIN core.polymers polymers ON polymers.polymer_id = properties.polymer_id
        WHERE {where_sql}
        ORDER BY properties.property_id ASC
        """,
        [*match_params, *match_params],
    ).fetchall()
    return total, list(rows)


def lookup_pi_candidate_smiles_postgres(
    connection: Any,
    *,
    query_smiles: str,
    canonical_smiles: str,
    limit: int = 50,
) -> tuple[int, list[dict[str, Any]]]:
    source_params = [
        canonical_smiles,
        query_smiles,
        canonical_smiles,
        query_smiles,
        canonical_smiles,
        query_smiles,
        canonical_smiles,
    ]
    where_sql = """
      p.canonical_polym = %s
      OR p.polym = %s
      OR p.polym = %s
      OR p.mon1 = %s
      OR p.mon1 = %s
      OR p.mon2 = %s
      OR p.mon2 = %s
    """
    total = int(
        connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM pi.polymers p
            JOIN pi.tg_predictions t ON t.id = p.id
            WHERE {where_sql}
            """,
            source_params,
        ).fetchone()["count"]
    )

    rows = connection.execute(
        f"""
        SELECT
          p.id AS pi_id,
          p.mon1,
          p.mon2,
          p.polym,
          p.canonical_polym,
          p.smiles_valid AS rdkit_parse_ok,
          t.tg_celsius,
          t.dielectric_const_dc,
          t.static_dielectric_const,
          t.dipole_debye,
          t.electrophilicity_index,
          t.homo_lumo_gap_ev,
          t.hardness,
          t.mulliken_electronegativity,
          t.redox_window_v,
          t.linear_expansion,
          t.refractive_index,
          CASE
            WHEN p.canonical_polym = %s THEN 'canonical_polym'
            WHEN p.polym = %s THEN 'polym'
            WHEN p.polym = %s THEN 'polym'
            WHEN p.mon1 = %s THEN 'mon1'
            WHEN p.mon1 = %s THEN 'mon1'
            WHEN p.mon2 = %s THEN 'mon2'
            WHEN p.mon2 = %s THEN 'mon2'
            ELSE 'polym'
          END AS source_column,
          CASE
            WHEN p.canonical_polym = %s THEN p.canonical_polym
            WHEN p.polym = %s THEN p.polym
            WHEN p.polym = %s THEN p.polym
            WHEN p.mon1 = %s THEN p.mon1
            WHEN p.mon1 = %s THEN p.mon1
            WHEN p.mon2 = %s THEN p.mon2
            WHEN p.mon2 = %s THEN p.mon2
            ELSE p.polym
          END AS matched_smiles
        FROM pi.polymers p
        JOIN pi.tg_predictions t ON t.id = p.id
        WHERE {where_sql}
        ORDER BY p.id ASC
        LIMIT %s
        """,
        [*source_params, *source_params, *source_params, limit],
    ).fetchall()
    return total, list(rows)


def browse_experimental_process_records_postgres(
    connection: Any,
    *,
    query: str,
    page: int,
    page_size: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    total_records = int(connection.execute("SELECT COUNT(*) AS count FROM experimental.process_records").fetchone()["count"])
    where_sql, params = _pg_like_where(query, EXPERIMENTAL_PROCESS_COLUMNS)
    offset = (page - 1) * page_size
    matched_records = int(
        connection.execute(
            f"SELECT COUNT(*) AS count FROM experimental.process_records {where_sql}",
            params,
        ).fetchone()["count"]
    )
    rows = connection.execute(
        f"""
        SELECT
          record_id,
          source_file,
          source_row_number,
          polymer_id,
          polymer_name,
          product_name,
          process_flow_original_text,
          material_original_text
        FROM experimental.process_records
        {where_sql}
        ORDER BY record_id ASC
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    ).fetchall()
    return total_records, matched_records, list(rows)


def browse_experimental_property_records_postgres(
    connection: Any,
    *,
    query: str,
    page: int,
    page_size: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    total_records = int(connection.execute("SELECT COUNT(*) AS count FROM experimental.property_records").fetchone()["count"])
    where_sql, params = _pg_like_where(query, EXPERIMENTAL_PROPERTY_COLUMNS)
    offset = (page - 1) * page_size
    matched_records = int(
        connection.execute(
            f"SELECT COUNT(*) AS count FROM experimental.property_records {where_sql}",
            params,
        ).fetchone()["count"]
    )
    rows = connection.execute(
        f"""
        SELECT
          record_id,
          source_file,
          source_row_number,
          polymer_id,
          polymer_name,
          property_category,
          property_name_en,
          value
        FROM experimental.property_records
        {where_sql}
        ORDER BY record_id ASC
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    ).fetchall()
    return total_records, matched_records, list(rows)


def browse_structure_property_records_postgres(
    connection: Any,
    *,
    query: str,
    page: int,
    page_size: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    total_records = int(connection.execute("SELECT COUNT(*) AS count FROM core.polymer_properties").fetchone()["count"])
    where_sql, params = _pg_like_where(query, STRUCTURE_PROPERTY_COLUMNS)
    offset = (page - 1) * page_size
    matched_records = int(
        connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM core.polymer_properties properties
            JOIN core.polymers polymers ON polymers.polymer_id = properties.polymer_id
            {where_sql}
            """,
            params,
        ).fetchone()["count"]
    )
    rows = connection.execute(
        f"""
        SELECT
          properties.property_id,
          properties.polymer_id,
          polymers.smiles,
          polymers.canonical_smiles,
          properties.property_category,
          properties.property_name,
          properties.property_value,
          properties.property_value_num,
          properties.property_unit,
          properties.label_source
        FROM core.polymer_properties properties
        JOIN core.polymers polymers ON polymers.polymer_id = properties.polymer_id
        {where_sql}
        ORDER BY properties.property_id ASC
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    ).fetchall()
    return total_records, matched_records, list(rows)


def get_property_filter_options_postgres(connection: Any) -> tuple[int, int, int, list[dict[str, Any]]]:
    summary = connection.execute(
        """
        SELECT
          COUNT(*) AS total_records,
          COUNT(*) FILTER (WHERE property_key IS NOT NULL) AS mapped_records,
          COUNT(*) FILTER (WHERE property_key IS NULL) AS raw_records
        FROM core.polymer_property_filter_records
        """
    ).fetchone()
    rows = connection.execute(
        """
        WITH standardized AS (
          SELECT
            'standardized'::text AS filter_type,
            'std:' || property_key || ':' || COALESCE(canonical_unit, '') AS option_key,
            COALESCE(NULLIF(MIN(NULLIF(property_label, '')), ''), property_key) AS label,
            property_key,
            NULL::text AS property_name,
            NULL::text AS property_unit_clean,
            canonical_unit,
            COUNT(*) AS rows,
            COUNT(DISTINCT COALESCE(NULLIF(smiles, ''), NULLIF(canonical_smiles, ''))) AS unique_smiles,
            MIN(canonical_value) AS min_value,
            percentile_cont(0.05) WITHIN GROUP (ORDER BY canonical_value) AS p5_value,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY canonical_value) AS median_value,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY canonical_value) AS p95_value,
            MAX(canonical_value) AS max_value
          FROM core.polymer_property_filter_records
          WHERE property_key IS NOT NULL
            AND canonical_value IS NOT NULL
          GROUP BY property_key, canonical_unit
        ),
        raw AS (
          SELECT
            'raw'::text AS filter_type,
            'raw:' || md5(property_name || '|' || COALESCE(property_unit_clean, '')) AS option_key,
            CASE
              WHEN COALESCE(NULLIF(property_unit_clean, ''), '') = '' THEN property_name
              ELSE property_name || ' (' || property_unit_clean || ')'
            END AS label,
            NULL::text AS property_key,
            property_name,
            property_unit_clean,
            NULL::text AS canonical_unit,
            COUNT(*) AS rows,
            COUNT(DISTINCT COALESCE(NULLIF(smiles, ''), NULLIF(canonical_smiles, ''))) AS unique_smiles,
            MIN(property_value_num) AS min_value,
            percentile_cont(0.05) WITHIN GROUP (ORDER BY property_value_num) AS p5_value,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY property_value_num) AS median_value,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY property_value_num) AS p95_value,
            MAX(property_value_num) AS max_value
          FROM core.polymer_property_filter_records
          WHERE property_key IS NULL
            AND property_value_num IS NOT NULL
          GROUP BY property_name, property_unit_clean
        )
        SELECT *
        FROM (
          SELECT * FROM standardized
          UNION ALL
          SELECT * FROM raw
        ) options
        ORDER BY
          CASE filter_type WHEN 'standardized' THEN 0 ELSE 1 END,
          rows DESC,
          label ASC
        """
    ).fetchall()
    return (
        int(summary["total_records"] or 0),
        int(summary["mapped_records"] or 0),
        int(summary["raw_records"] or 0),
        list(rows),
    )


def _property_filter_search_clause(query: str, params: list[Any]) -> str:
    normalized = query.strip()
    if not normalized:
        return ""
    like_value = f"%{_escape_like(normalized)}%"
    params.extend([like_value, like_value, like_value])
    return (
        " AND (COALESCE(smiles, '') ILIKE %s ESCAPE '\\' "
        "OR COALESCE(canonical_smiles, '') ILIKE %s ESCAPE '\\' "
        "OR COALESCE(polymer_name, '') ILIKE %s ESCAPE '\\')"
    )


def _property_filter_condition_select(filter_index: int, condition: Any, query: str) -> tuple[str, list[Any]]:
    params: list[Any] = []
    where_parts: list[str] = []
    if condition.filter_type == "standardized":
        where_parts.append("property_key = %s")
        params.append(condition.property_key)
        where_parts.append("canonical_value IS NOT NULL")
        canonical_unit = getattr(condition, "canonical_unit", None)
        if canonical_unit is not None:
            where_parts.append("COALESCE(canonical_unit, '') = %s")
            params.append(canonical_unit or "")
        if condition.min_value is not None:
            where_parts.append("canonical_value >= %s")
            params.append(condition.min_value)
        if condition.max_value is not None:
            where_parts.append("canonical_value <= %s")
            params.append(condition.max_value)
    else:
        where_parts.append("property_key IS NULL")
        where_parts.append("property_name = %s")
        params.append(condition.property_name)
        where_parts.append("COALESCE(property_unit_clean, '') = %s")
        params.append(condition.property_unit_clean or "")
        where_parts.append("property_value_num IS NOT NULL")
        if condition.min_value is not None:
            where_parts.append("property_value_num >= %s")
            params.append(condition.min_value)
        if condition.max_value is not None:
            where_parts.append("property_value_num <= %s")
            params.append(condition.max_value)
    search_clause = _property_filter_search_clause(query, params)
    where_sql = " AND ".join(where_parts)
    return (
        f"""
        SELECT
          %s::int AS filter_index,
          COALESCE(NULLIF(smiles, ''), NULLIF(canonical_smiles, ''), 'record:' || filter_record_id::text) AS group_key,
          {PROPERTY_FILTER_SELECT_COLUMNS}
        FROM core.polymer_property_filter_records
        WHERE {where_sql}
        {search_clause}
        """,
        [filter_index, *params],
    )


def search_property_filter_records_postgres(
    connection: Any,
    *,
    conditions: list[Any],
    query: str,
    page: int,
    page_size: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    total_records = int(connection.execute("SELECT COUNT(*) AS count FROM core.polymer_property_filter_records").fetchone()["count"])
    candidate_sql_parts: list[str] = []
    candidate_params: list[Any] = []
    for filter_index, condition in enumerate(conditions):
        condition_sql, condition_params = _property_filter_condition_select(filter_index, condition, query)
        candidate_sql_parts.append(condition_sql)
        candidate_params.extend(condition_params)
    candidate_sql = "\nUNION ALL\n".join(candidate_sql_parts)
    filter_count = len(conditions)
    matched_records = int(
        connection.execute(
            f"""
            WITH candidate_matches AS (
              {candidate_sql}
            ),
            matched_groups AS (
              SELECT group_key
              FROM candidate_matches
              GROUP BY group_key
              HAVING COUNT(DISTINCT filter_index) = %s
            )
            SELECT COUNT(*) AS count
            FROM matched_groups
            """,
            [*candidate_params, filter_count],
        ).fetchone()["count"]
    )
    offset = (page - 1) * page_size
    rows = connection.execute(
        f"""
        WITH candidate_matches AS (
          {candidate_sql}
        ),
        matched_groups AS (
          SELECT group_key
          FROM candidate_matches
          GROUP BY group_key
          HAVING COUNT(DISTINCT filter_index) = %s
        ),
        page_groups AS (
          SELECT group_key
          FROM matched_groups
          ORDER BY group_key ASC
          LIMIT %s OFFSET %s
        )
        SELECT candidate_matches.*
        FROM candidate_matches
        JOIN page_groups ON page_groups.group_key = candidate_matches.group_key
        ORDER BY
          candidate_matches.group_key ASC,
          candidate_matches.filter_index ASC,
          CASE lower(COALESCE(candidate_matches.value_origin, ''))
            WHEN 'observed' THEN 0
            WHEN 'median' THEN 1
            WHEN 'impute' THEN 2
            ELSE 3
          END ASC,
          candidate_matches.reliable_score DESC NULLS LAST,
          candidate_matches.filter_record_id ASC
        """,
        [*candidate_params, filter_count, page_size, offset],
    ).fetchall()
    return total_records, matched_records, list(rows)


def get_dft_browser_summary_postgres(connection: Any) -> tuple[int, int, float, int]:
    molecule_count = int(connection.execute("SELECT COUNT(*) AS count FROM dft.molecule_final").fetchone()["count"])
    step_count = int(connection.execute("SELECT COUNT(*) AS count FROM dft.energy_trace").fetchone()["count"])
    row = connection.execute(
        """
        SELECT AVG(final_step + 1) AS average_steps, MAX(final_step + 1) AS max_steps
        FROM dft.molecule_final
        """
    ).fetchone()
    return molecule_count, step_count, float(row["average_steps"] or 0.0), int(row["max_steps"] or 0)


def browse_dft_molecules_postgres(
    connection: Any,
    *,
    query: str,
    page: int,
    page_size: int,
) -> tuple[int, int, int, float, int, list[dict[str, Any]]]:
    total_records, total_step_records, average_steps, max_steps = get_dft_browser_summary_postgres(connection)
    where_sql, params = _pg_like_where(query, DFT_MOLECULE_COLUMNS)
    offset = (page - 1) * page_size
    matched_records = int(
        connection.execute(
            f"SELECT COUNT(*) AS count FROM dft.molecule_final {where_sql}",
            params,
        ).fetchone()["count"]
    )
    rows = connection.execute(
        f"""
        SELECT
          mol_id,
          range_group,
          final_step,
          n_atoms,
          final_step + 1 AS trace_points,
          scf_energy,
          zero_point_energy,
          thermal_enthalpy,
          gibbs_free_energy,
          lowest_freq,
          dipole_moment,
          homo_ev,
          lumo_ev,
          gap_ev,
          is_converged
        FROM dft.molecule_final
        {where_sql}
        ORDER BY mol_id ASC
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    ).fetchall()
    return total_records, matched_records, total_step_records, average_steps, max_steps, list(rows)


def browse_dft_energy_steps_postgres(
    connection: Any,
    *,
    query: str,
    mol_id: str | None,
    page: int,
    page_size: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    total_records = int(connection.execute("SELECT COUNT(*) AS count FROM dft.energy_trace").fetchone()["count"])
    exact_mol_id = mol_id.strip() if mol_id else ""
    if exact_mol_id:
        where_sql = "WHERE mol_id = %s"
        params = [exact_mol_id]
    else:
        where_sql, params = _pg_like_where(query, DFT_STEP_COLUMNS)
    offset = (page - 1) * page_size
    matched_records = int(
        connection.execute(
            f"SELECT COUNT(*) AS count FROM dft.energy_trace {where_sql}",
            params,
        ).fetchone()["count"]
    )
    rows = connection.execute(
        f"""
        SELECT mol_id, step, scf_energy, homo_ev, lumo_ev, gap_ev
        FROM dft.energy_trace
        {where_sql}
        ORDER BY mol_id ASC, step ASC
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    ).fetchall()
    return total_records, matched_records, list(rows)


def browse_formulation_records_postgres(
    connection: Any,
    *,
    query: str,
    page: int,
    page_size: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    total_records = int(connection.execute("SELECT COUNT(*) AS count FROM knowledge.formulation_records").fetchone()["count"])
    where_sql, params = _pg_like_where(query, FORMULATION_COLUMNS)
    offset = (page - 1) * page_size
    matched_records = int(
        connection.execute(
            f"SELECT COUNT(*) AS count FROM knowledge.formulation_records {where_sql}",
            params,
        ).fetchone()["count"]
    )
    rows = connection.execute(
        f"""
        SELECT
          formulation_id,
          knowledge_id,
          source_file,
          source_row_number,
          polymer_iupac,
          formulation,
          catalyst,
          temperature,
          reaction_time,
          solvent
        FROM knowledge.formulation_records
        {where_sql}
        ORDER BY formulation_id ASC
        LIMIT %s OFFSET %s
        """,
        [*params, page_size, offset],
    ).fetchall()
    return total_records, matched_records, list(rows)


COLOR_PALETTE = ("#0f766e", "#2563eb", "#f59e0b", "#e11d48", "#7c3aed", "#64748b", "#14b8a6", "#38bdf8")
COMMON_STOPWORDS = (
    "the", "and", "for", "with", "from", "that", "this", "were", "was", "are", "into", "after",
    "before", "using", "then", "been", "their", "which", "polymer", "polymers", "material", "materials",
    "solution", "sample", "samples",
)
STOPWORD_SQL = ", ".join(f"'{word}'" for word in COMMON_STOPWORDS)


def _int_value(value: Any) -> int:
    return int(value or 0)


def _float_value(value: Any) -> float:
    return float(value or 0.0)


def _ranked_metric_rows(connection: Any, sql: str, params: tuple[Any, ...] = (), *, colors: bool = False) -> list[dict[str, Any]]:
    rows = connection.execute(sql, params).fetchall()
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        label = str(row["label"] or "not labeled")
        item: dict[str, Any] = {"label": label, "value": _int_value(row["value"])}
        if colors:
            item["color"] = COLOR_PALETTE[index % len(COLOR_PALETTE)]
        results.append(item)
    return results


def _top_terms(connection: Any, table_sql: str, expression_sql: str, *, limit: int = 10) -> list[dict[str, Any]]:
    return _ranked_metric_rows(
        connection,
        f"""
        WITH terms AS (
          SELECT regexp_split_to_table(lower(COALESCE({expression_sql}, '')), '[^[:alnum:]+-]+') AS label
          FROM {table_sql}
        )
        SELECT label, COUNT(*) AS value
        FROM terms
        WHERE length(label) > 2
          AND label NOT IN ({STOPWORD_SQL})
          AND label !~ '^[0-9]+$'
        GROUP BY label
        ORDER BY value DESC, label ASC
        LIMIT %s
        """,
        (limit,),
    )


def _numeric_range_rows(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    rows = connection.execute(sql, params).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "label": str(row["label"] or "not labeled"),
            "count": _int_value(row["count"]),
            "min": _float_value(row["min"]),
            "median": _float_value(row["median"]),
            "max": _float_value(row["max"]),
        }
        if "p5" in row:
            item["p5"] = _float_value(row["p5"])
        if "p95" in row:
            item["p95"] = _float_value(row["p95"])
        results.append(item)
    return results


def _single_numeric_range(connection: Any, table_sql: str, value_sql: str) -> dict[str, Any]:
    row = connection.execute(
        f"""
        SELECT
          COUNT({value_sql}) AS count,
          MIN({value_sql}) AS min,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY {value_sql}) AS median,
          MAX({value_sql}) AS max
        FROM {table_sql}
        WHERE {value_sql} IS NOT NULL
        """
    ).fetchone()
    return {
        "count": _int_value(row["count"]),
        "min": _float_value(row["min"]),
        "median": _float_value(row["median"]),
        "max": _float_value(row["max"]),
    }


def _numeric_distribution(connection: Any, *, label: str, color: str, table_sql: str, value_sql: str, bins: int = 12) -> dict[str, Any]:
    stats = connection.execute(
        f"""
        SELECT
          COUNT({value_sql}) AS count,
          MIN({value_sql}) AS min,
          percentile_cont(0.05) WITHIN GROUP (ORDER BY {value_sql}) AS p5,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY {value_sql}) AS median,
          percentile_cont(0.95) WITHIN GROUP (ORDER BY {value_sql}) AS p95,
          MAX({value_sql}) AS max
        FROM {table_sql}
        WHERE {value_sql} IS NOT NULL
        """
    ).fetchone()
    count = _int_value(stats["count"])
    minimum = _float_value(stats["min"])
    maximum = _float_value(stats["max"])
    distribution: dict[str, Any] = {
        "label": label,
        "color": color,
        "count": count,
        "min": minimum,
        "p5": _float_value(stats["p5"]),
        "median": _float_value(stats["median"]),
        "p95": _float_value(stats["p95"]),
        "max": maximum,
        "bins": [],
    }
    if count == 0:
        return distribution
    if minimum == maximum:
        distribution["bins"] = [{"start": minimum, "end": maximum, "value": count}]
        return distribution

    bucket_rows = connection.execute(
        f"""
        SELECT width_bucket({value_sql}, %s, %s, %s) AS bucket, COUNT(*) AS value
        FROM {table_sql}
        WHERE {value_sql} IS NOT NULL
        GROUP BY bucket
        ORDER BY bucket
        """,
        (minimum, maximum, bins),
    ).fetchall()
    counts = [0 for _ in range(bins)]
    for row in bucket_rows:
        bucket = max(1, min(int(row["bucket"]), bins))
        counts[bucket - 1] += _int_value(row["value"])
    span = maximum - minimum
    distribution["bins"] = [
        {"start": minimum + span * index / bins, "end": minimum + span * (index + 1) / bins, "value": value}
        for index, value in enumerate(counts)
    ]
    return distribution


def _count_where(connection: Any, table_sql: str, where_sql: str) -> int:
    return _int_value(connection.execute(f"SELECT COUNT(*) AS count FROM {table_sql} WHERE {where_sql}").fetchone()["count"])


def _property_filter_analytics(connection: Any) -> dict[str, Any]:
    summary = connection.execute(
        """
        SELECT
          COUNT(*) AS rows,
          COUNT(*) FILTER (WHERE property_key IS NOT NULL) AS mapped_rows,
          COUNT(*) FILTER (WHERE property_key IS NULL) AS raw_rows,
          COUNT(DISTINCT property_key) FILTER (WHERE property_key IS NOT NULL) AS standardized_properties,
          COUNT(DISTINCT property_name) FILTER (WHERE property_key IS NULL) AS raw_properties,
          COUNT(DISTINCT COALESCE(NULLIF(smiles, ''), NULLIF(canonical_smiles, ''))) AS unique_smiles
        FROM core.polymer_property_filter_records
        """
    ).fetchone()
    return {
        "rows": _int_value(summary["rows"]),
        "mappedRows": _int_value(summary["mapped_rows"]),
        "rawRows": _int_value(summary["raw_rows"]),
        "standardizedProperties": _int_value(summary["standardized_properties"]),
        "rawProperties": _int_value(summary["raw_properties"]),
        "uniqueSmiles": _int_value(summary["unique_smiles"]),
    }


def _process_analytics(connection: Any) -> dict[str, Any]:
    summary = connection.execute(
        """
        SELECT
          COUNT(*) AS rows,
          COUNT(DISTINCT source_file || ':' || source_row_number::text) AS unique_record_ids,
          COUNT(DISTINCT NULLIF(polymer_name, '')) AS unique_polymers,
          COUNT(DISTINCT NULLIF(product_name, '')) AS unique_products,
          AVG(length(COALESCE(process_flow_original_text, ''))) AS avg_process_text_length,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY length(COALESCE(process_flow_original_text, ''))) AS median_chars
        FROM experimental.process_records
        """
    ).fetchone()
    total_signal_rows = _int_value(summary["rows"])
    process_signals = []
    for label in ("temperature", "time", "solvent", "dried", "added", "stirred", "vacuum", "washed"):
        row = connection.execute(
            "SELECT COUNT(*) AS value FROM experimental.process_records WHERE process_flow_original_text ILIKE %s",
            (f"%{label}%",),
        ).fetchone()
        process_signals.append({"label": label, "value": _int_value(row["value"]), "total": total_signal_rows})

    return {
        "rows": _int_value(summary["rows"]),
        "uniqueRecordIds": _int_value(summary["unique_record_ids"]),
        "uniquePolymers": _int_value(summary["unique_polymers"]),
        "uniqueProducts": _int_value(summary["unique_products"]),
        "avgProcessTextLength": round(_float_value(summary["avg_process_text_length"]), 1),
        "processSignalSummary": {
            "extractedRows": sum(item["value"] for item in process_signals),
            "uniqueSnippets": total_signal_rows,
            "medianChars": round(_float_value(summary["median_chars"])),
        },
        "processSignals": process_signals,
        "topTerms": _top_terms(connection, "experimental.process_records", "process_flow_original_text || ' ' || material_original_text", limit=10),
        "topProducts": _ranked_metric_rows(
            connection,
            """
            SELECT NULLIF(TRIM(product_name), '') AS label, COUNT(*) AS value
            FROM experimental.process_records
            WHERE NULLIF(TRIM(product_name), '') IS NOT NULL
            GROUP BY label
            ORDER BY value DESC, label ASC
            LIMIT 8
            """,
        ),
        "topMaterials": _top_terms(connection, "experimental.process_records", "material_original_text", limit=8),
    }


def _property_analytics(connection: Any) -> dict[str, Any]:
    summary = connection.execute(
        """
        SELECT
          COUNT(*) AS rows,
          COUNT(DISTINCT NULLIF(polymer_id, '')) AS unique_polymers,
          COUNT(DISTINCT NULLIF(property_name_en, '')) AS unique_properties
        FROM experimental.property_records
        """
    ).fetchone()
    number_sql = "NULLIF(substring(value from '[-+]?[0-9]+[.]?[0-9]*'), '')::double precision"
    return {
        "rows": _int_value(summary["rows"]),
        "uniquePolymers": _int_value(summary["unique_polymers"]),
        "uniqueProperties": _int_value(summary["unique_properties"]),
        "categories": _ranked_metric_rows(
            connection,
            """
            SELECT lower(COALESCE(NULLIF(TRIM(property_category), ''), 'other')) AS label, COUNT(*) AS value
            FROM experimental.property_records
            GROUP BY label
            ORDER BY value DESC, label ASC
            LIMIT 8
            """,
            colors=True,
        ),
        "topProperties": _ranked_metric_rows(
            connection,
            """
            SELECT NULLIF(TRIM(property_name_en), '') AS label, COUNT(*) AS value
            FROM experimental.property_records
            WHERE NULLIF(TRIM(property_name_en), '') IS NOT NULL
            GROUP BY label
            ORDER BY value DESC, label ASC
            LIMIT 8
            """,
        ),
        "ranges": _numeric_range_rows(
            connection,
            f"""
            WITH parsed AS (
              SELECT property_name_en AS label, {number_sql} AS numeric_value
              FROM experimental.property_records
              WHERE NULLIF(TRIM(property_name_en), '') IS NOT NULL
                AND substring(value from '[-+]?[0-9]+[.]?[0-9]*') IS NOT NULL
            )
            SELECT label, COUNT(*) AS count, MIN(numeric_value) AS min,
              percentile_cont(0.05) WITHIN GROUP (ORDER BY numeric_value) AS p5,
              percentile_cont(0.5) WITHIN GROUP (ORDER BY numeric_value) AS median,
              percentile_cont(0.95) WITHIN GROUP (ORDER BY numeric_value) AS p95,
              MAX(numeric_value) AS max
            FROM parsed
            GROUP BY label
            HAVING COUNT(*) >= 5
            ORDER BY count DESC, label ASC
            LIMIT 5
            """,
        ),
        "categoryTop": _ranked_metric_rows(
            connection,
            """
            WITH counts AS (
              SELECT lower(COALESCE(NULLIF(TRIM(property_category), ''), 'other')) AS category,
                     NULLIF(TRIM(property_name_en), '') AS property,
                     COUNT(*) AS value
              FROM experimental.property_records
              WHERE NULLIF(TRIM(property_name_en), '') IS NOT NULL
              GROUP BY category, property
            ), ranked AS (
              SELECT *, row_number() OVER (PARTITION BY category ORDER BY value DESC, property ASC) AS rn
              FROM counts
            )
            SELECT category || ': ' || property AS label, value
            FROM ranked
            WHERE rn = 1
            ORDER BY value DESC
            LIMIT 8
            """,
        ),
    }


def _structure_effect_analytics(connection: Any) -> dict[str, Any]:
    summary = connection.execute("SELECT COUNT(*) AS rows, COUNT(DISTINCT polymer_id) AS unique_smiles FROM core.polymer_properties").fetchone()
    ranges = _numeric_range_rows(
        connection,
        """
        SELECT property_name AS label, COUNT(*) AS count, MIN(property_value_num) AS min,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY property_value_num) AS median,
          MAX(property_value_num) AS max
        FROM core.polymer_properties
        WHERE property_value_num IS NOT NULL
        GROUP BY property_name
        HAVING COUNT(*) >= 5
        ORDER BY count DESC, label ASC
        LIMIT 5
        """,
    )
    return {
        "rows": _int_value(summary["rows"]),
        "uniqueSmiles": _int_value(summary["unique_smiles"]),
        "properties": _ranked_metric_rows(connection, "SELECT property_name AS label, COUNT(*) AS value FROM core.polymer_properties GROUP BY label ORDER BY value DESC, label ASC LIMIT 9"),
        "units": _ranked_metric_rows(
            connection,
            "SELECT COALESCE(NULLIF(TRIM(property_unit), ''), 'not labeled') AS label, COUNT(*) AS value FROM core.polymer_properties GROUP BY label ORDER BY value DESC, label ASC LIMIT 7",
            colors=True,
        ),
        "sources": _ranked_metric_rows(
            connection,
            "SELECT COALESCE(NULLIF(TRIM(label_source), ''), 'not labeled') AS label, COUNT(*) AS value FROM core.polymer_properties GROUP BY label ORDER BY value DESC, label ASC LIMIT 6",
            colors=True,
        ),
        "sourceMatrix": [
            {"label": str(row["label"]), "exp": _int_value(row["exp"]), "sim": _int_value(row["sim"]), "na": _int_value(row["na"])}
            for row in connection.execute(
                """
                WITH top_properties AS (
                  SELECT property_name FROM core.polymer_properties GROUP BY property_name ORDER BY COUNT(*) DESC, property_name ASC LIMIT 9
                )
                SELECT p.property_name AS label,
                  SUM(CASE WHEN lower(COALESCE(p.label_source, '')) IN ('exp', 'experimental') THEN 1 ELSE 0 END) AS exp,
                  SUM(CASE WHEN lower(COALESCE(p.label_source, '')) IN ('sim', 'simulated') THEN 1 ELSE 0 END) AS sim,
                  SUM(CASE WHEN COALESCE(NULLIF(TRIM(p.label_source), ''), '') = '' THEN 1 ELSE 0 END) AS na
                FROM core.polymer_properties p
                JOIN top_properties t ON t.property_name = p.property_name
                GROUP BY p.property_name
                ORDER BY COUNT(*) DESC, p.property_name ASC
                """
            ).fetchall()
        ],
        "ranges": ranges,
    }


def _dft_analytics(connection: Any) -> dict[str, Any]:
    summary = connection.execute("SELECT (SELECT COUNT(*) FROM dft.energy_trace) AS rows, COUNT(*) AS mol_count FROM dft.molecule_final").fetchone()
    return {
        "rows": _int_value(summary["rows"]),
        "molCount": _int_value(summary["mol_count"]),
        "energyRange": _single_numeric_range(connection, "dft.molecule_final", "scf_energy"),
        "gapRange": _single_numeric_range(connection, "dft.molecule_final", "gap_ev"),
        "orbitalDistributions": [
            _numeric_distribution(connection, label="HOMO", color=COLOR_PALETTE[0], table_sql="dft.molecule_final", value_sql="homo_ev"),
            _numeric_distribution(connection, label="LUMO", color=COLOR_PALETTE[1], table_sql="dft.molecule_final", value_sql="lumo_ev"),
        ],
        "stepRange": _single_numeric_range(connection, "dft.molecule_final", "final_step + 1"),
        "atomRange": _single_numeric_range(connection, "dft.molecule_final", "n_atoms"),
        "atomTotals": _ranked_metric_rows(
            connection,
            """
            SELECT CASE (atom->>0)::int
                WHEN 1 THEN 'H' WHEN 5 THEN 'B' WHEN 6 THEN 'C' WHEN 7 THEN 'N' WHEN 8 THEN 'O'
                WHEN 9 THEN 'F' WHEN 14 THEN 'Si' WHEN 15 THEN 'P' WHEN 16 THEN 'S' WHEN 17 THEN 'Cl'
                ELSE (atom->>0)
              END AS label,
              COUNT(*) AS value
            FROM dft.molecule_final
            CROSS JOIN LATERAL jsonb_array_elements(coordinates::jsonb) AS atom
            GROUP BY label
            ORDER BY value DESC, label ASC
            LIMIT 12
            """,
            colors=True,
        ),
        "convergence": _ranked_metric_rows(
            connection,
            "SELECT COALESCE(NULLIF(TRIM(is_converged), ''), 'blank') AS label, COUNT(*) AS value FROM dft.molecule_final GROUP BY label ORDER BY value DESC, label ASC LIMIT 8",
            colors=True,
        ),
    }


def _formulation_analytics(connection: Any) -> dict[str, Any]:
    total = _int_value(connection.execute("SELECT COUNT(*) AS count FROM knowledge.formulation_records").fetchone()["count"])
    files = _int_value(connection.execute("SELECT COUNT(DISTINCT source_file) AS count FROM knowledge.formulation_records").fetchone()["count"])

    def coverage_item(label: str, column: str) -> dict[str, Any]:
        count = _count_where(connection, "knowledge.formulation_records", f"NULLIF(TRIM({column}), '') IS NOT NULL")
        return {"label": label, "count": count, "pct": round((count / total) * 100, 1) if total else 0.0}

    component_rows = connection.execute(
        """
        WITH counts AS (
          SELECT CASE
            WHEN NULLIF(TRIM(formulation), '') IS NULL THEN 0
            ELSE LEAST(8, GREATEST(1, cardinality(regexp_split_to_array(formulation, ';|\\+|,|\\sand\\s'))))
          END AS component_count
          FROM knowledge.formulation_records
        ), labeled AS (
          SELECT CASE WHEN component_count = 0 THEN 'missing' WHEN component_count = 8 THEN '8+' ELSE component_count::text END AS label
          FROM counts
        )
        SELECT label, COUNT(*) AS value
        FROM labeled
        GROUP BY label
        ORDER BY CASE WHEN label = 'missing' THEN 99 WHEN label = '8+' THEN 8 ELSE label::int END
        """
    ).fetchall()
    temp_rows = connection.execute(
        """
        WITH parsed AS (
          SELECT NULLIF(substring(temperature from '[-+]?[0-9]+[.]?[0-9]*'), '')::double precision AS temp
          FROM knowledge.formulation_records
        )
        SELECT '<80 C' AS label, COUNT(*) AS value FROM parsed WHERE temp < 80
        UNION ALL SELECT '80-119 C', COUNT(*) FROM parsed WHERE temp >= 80 AND temp < 120
        UNION ALL SELECT '120-179 C', COUNT(*) FROM parsed WHERE temp >= 120 AND temp < 180
        UNION ALL SELECT '180-239 C', COUNT(*) FROM parsed WHERE temp >= 180 AND temp < 240
        UNION ALL SELECT '>=240 C', COUNT(*) FROM parsed WHERE temp >= 240
        UNION ALL SELECT 'missing', COUNT(*) FROM parsed WHERE temp IS NULL
        """
    ).fetchall()
    examples = [
        {
            "title": row["title"] or "Untitled formulation record",
            "polymer": row["polymer"] or "polymer not specified",
            "formula": row["formula"] or "formulation not specified",
            "condition": row["condition"] or "condition not specified",
        }
        for row in connection.execute(
            """
            SELECT COALESCE(NULLIF(d.title_en, ''), NULLIF(d.title_zh, ''), fr.source_file) AS title,
                   fr.polymer_iupac AS polymer,
                   fr.formulation AS formula,
                   NULLIF(concat_ws(' · ', NULLIF(fr.temperature, ''), NULLIF(fr.reaction_time, ''), NULLIF(fr.solvent, '')), '') AS condition
            FROM knowledge.formulation_records fr
            LEFT JOIN knowledge.documents d ON d.knowledge_id = fr.knowledge_id
            WHERE NULLIF(TRIM(fr.formulation), '') IS NOT NULL
            ORDER BY fr.formulation_id ASC
            LIMIT 2
            """
        ).fetchall()
    ]
    return {
        "files": files,
        "rows": total,
        "coverage": [
            coverage_item("Polymer IUPAC", "polymer_iupac"), coverage_item("Formula and dosage", "formulation"), coverage_item("Catalyst", "catalyst"),
            coverage_item("Temperature", "temperature"), coverage_item("Time", "reaction_time"), coverage_item("Solvent", "solvent"),
        ],
        "componentCounts": [{"label": str(row["label"]), "value": _int_value(row["value"])} for row in component_rows],
        "topComponents": _top_terms(connection, "knowledge.formulation_records", "formulation", limit=12),
        "polymerFamilies": _ranked_metric_rows(
            connection,
            """
            SELECT CASE
                WHEN lower(COALESCE(polymer_iupac, '')) LIKE '%%polyurethane%%' THEN 'polyurethane'
                WHEN lower(COALESCE(polymer_iupac, '')) LIKE '%%polyester%%' THEN 'polyester'
                WHEN lower(COALESCE(polymer_iupac, '')) LIKE '%%polyamide%%' OR lower(COALESCE(polymer_iupac, '')) LIKE '%%aramid%%' THEN 'polyamide / aramid'
                WHEN lower(COALESCE(polymer_iupac, '')) LIKE '%%epoxy%%' THEN 'epoxy'
                WHEN lower(COALESCE(polymer_iupac, '')) LIKE '%%acryl%%' THEN 'acrylic'
                WHEN lower(COALESCE(polymer_iupac, '')) LIKE '%%imide%%' THEN 'polyimide'
                WHEN lower(COALESCE(polymer_iupac, '')) LIKE '%%olefin%%' OR lower(COALESCE(polymer_iupac, '')) LIKE '%%ethylene%%' THEN 'polyolefin'
                ELSE 'other'
              END AS label,
              COUNT(*) AS value
            FROM knowledge.formulation_records
            GROUP BY label
            ORDER BY value DESC, label ASC
            LIMIT 8
            """,
            colors=True,
        ),
        "ratioTypes": [
            {"label": "ratio colon", "value": _count_where(connection, "knowledge.formulation_records", "formulation ~ ':'")},
            {"label": "range", "value": _count_where(connection, "knowledge.formulation_records", "formulation ~ '[0-9]\\s*[-–]\\s*[0-9]'")},
            {"label": "not specified", "value": _count_where(connection, "knowledge.formulation_records", "NULLIF(TRIM(formulation), '') IS NOT NULL AND formulation !~ ':'")},
            {"label": "missing", "value": _count_where(connection, "knowledge.formulation_records", "NULLIF(TRIM(formulation), '') IS NULL")},
            {"label": "percent", "value": _count_where(connection, "knowledge.formulation_records", "position('%' in formulation) > 0 OR formulation ILIKE '%% percent%%'")},
            {"label": "molar", "value": _count_where(connection, "knowledge.formulation_records", "formulation ILIKE '%%mol%%'")},
            {"label": "parts / wt", "value": _count_where(connection, "knowledge.formulation_records", "formulation ILIKE '%%wt%%' OR formulation ILIKE '%%parts%%'")},
        ],
        "tempBands": [{"label": str(row["label"]), "value": _int_value(row["value"])} for row in temp_rows],
        "timeUnits": [
            {"label": "hours", "value": _count_where(connection, "knowledge.formulation_records", "reaction_time ~* '(h|hour)'"), "color": COLOR_PALETTE[0]},
            {"label": "minutes", "value": _count_where(connection, "knowledge.formulation_records", "reaction_time ~* '(min|minute)'"), "color": COLOR_PALETTE[1]},
            {"label": "days", "value": _count_where(connection, "knowledge.formulation_records", "reaction_time ~* '(d|day)'"), "color": COLOR_PALETTE[2]},
            {"label": "other", "value": _count_where(connection, "knowledge.formulation_records", "NULLIF(TRIM(reaction_time), '') IS NOT NULL AND reaction_time !~* '(h|hour|min|minute|d|day)'"), "color": COLOR_PALETTE[4]},
            {"label": "missing", "value": _count_where(connection, "knowledge.formulation_records", "NULLIF(TRIM(reaction_time), '') IS NULL"), "color": COLOR_PALETTE[5]},
        ],
        "topCatalysts": _ranked_metric_rows(connection, "SELECT NULLIF(TRIM(catalyst), '') AS label, COUNT(*) AS value FROM knowledge.formulation_records WHERE NULLIF(TRIM(catalyst), '') IS NOT NULL GROUP BY label ORDER BY value DESC, label ASC LIMIT 10"),
        "topSolvents": _ranked_metric_rows(connection, "SELECT NULLIF(TRIM(solvent), '') AS label, COUNT(*) AS value FROM knowledge.formulation_records WHERE NULLIF(TRIM(solvent), '') IS NOT NULL GROUP BY label ORDER BY value DESC, label ASC LIMIT 10"),
        "examples": examples,
    }


def get_database_analytics_postgres(connection: Any) -> dict[str, Any]:
    return {
        "process": _process_analytics(connection),
        "property": _property_analytics(connection),
        "structureEffect": _structure_effect_analytics(connection),
        "propertyFilter": _property_filter_analytics(connection),
        "dft": _dft_analytics(connection),
        "formulation": _formulation_analytics(connection),
    }


def source_file_status(connection: Any, logical_name: str) -> tuple[str, str | None]:
    if not postgres_table_exists(connection, "governance", "source_files"):
        return "unknown", None
    row = connection.execute(
        """
        SELECT status, notes
        FROM governance.source_files
        WHERE logical_name = %s
        """,
        (logical_name,),
    ).fetchone()
    if row is None:
        return "unknown", None
    return str(row["status"]), row["notes"]
