from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from common import (
    RAW_DATA_DIR,
    RESULTS_DIR,
    ensure_dir,
    load_raw_datasets,
    missing_summary,
    normalize_position_group,
    parse_date,
    round_frame,
    safe_numeric,
    season_anchor_year,
    write_json,
)


def clean_profiles(df: pd.DataFrame) -> pd.DataFrame:
    profiles = df.copy()
    profiles = profiles.drop_duplicates(subset=["player_id"], keep="first")
    for column in ["birth_date", "club_since", "contract_until", "last_contract_extension", "current_market_value_date"]:
        profiles[column] = parse_date(profiles[column])
    for column in ["age_current", "current_market_value_eur"]:
        profiles[column] = safe_numeric(profiles[column])
    profiles["position_group"] = profiles["position_group"].map(normalize_position_group)
    profiles["nationality"] = profiles["nationality"].astype("string").str.strip()
    profiles["position_detail"] = profiles["position_detail"].astype("string").str.strip()
    profiles["is_brazilian"] = profiles["nationality"].str.contains("Brasil|Brazil", case=False, na=False)
    return profiles


def clean_market_values(df: pd.DataFrame) -> pd.DataFrame:
    market_values = df.copy()
    market_values["valuation_date"] = parse_date(market_values["valuation_date"])
    market_values["market_value_eur"] = safe_numeric(market_values["market_value_eur"])
    market_values["club_at_valuation"] = market_values["club_at_valuation"].astype("string").str.strip()
    market_values = market_values.drop_duplicates(subset=["player_id", "valuation_date"], keep="first")
    market_values = market_values.loc[
        market_values["valuation_date"].notna() & market_values["market_value_eur"].notna() & (market_values["market_value_eur"] >= 0)
    ].copy()
    return market_values


def clean_injuries(df: pd.DataFrame) -> pd.DataFrame:
    injuries = df.copy()
    injuries["from_date"] = parse_date(injuries["from_date"])
    injuries["to_date"] = parse_date(injuries["to_date"])
    injuries["days_out"] = safe_numeric(injuries["days_out"])
    injuries["games_missed"] = safe_numeric(injuries["games_missed"])
    injuries["injury"] = injuries["injury"].astype("string").str.strip()
    inferred_to = injuries["from_date"] + pd.to_timedelta(injuries["days_out"].fillna(0), unit="D")
    injuries["to_date_filled"] = injuries["to_date"].fillna(inferred_to)
    injuries["severe_injury"] = injuries["days_out"].fillna(0) >= 60
    injuries = injuries.drop_duplicates(
        subset=["player_id", "injury", "from_date", "to_date_filled"],
        keep="first",
    )
    return injuries


def clean_transfers(df: pd.DataFrame) -> pd.DataFrame:
    transfers = df.copy()
    transfers["transfer_date"] = parse_date(transfers["transfer_date"], utc=True)
    for column in ["age_at_transfer", "market_value_eur", "transfer_fee_eur"]:
        transfers[column] = safe_numeric(transfers[column])
    transfers["contract_until"] = parse_date(transfers["contract_until"])
    transfers["international_change"] = (
        transfers["from_country_id"].notna()
        & transfers["to_country_id"].notna()
        & (transfers["from_country_id"].astype("string") != transfers["to_country_id"].astype("string"))
    )
    transfers["competition_change"] = (
        transfers["from_competition_id"].notna()
        & transfers["to_competition_id"].notna()
        & (transfers["from_competition_id"].astype("string") != transfers["to_competition_id"].astype("string"))
    )
    transfers = transfers.drop_duplicates(subset=["player_id", "transfer_id"], keep="first")
    transfers = transfers.loc[transfers["transfer_date"].notna()].copy()
    return transfers


def clean_performance(df: pd.DataFrame) -> pd.DataFrame:
    perf = df.copy()
    perf["tm_season_id"] = safe_numeric(perf["tm_season_id"])
    perf["minutes_est"] = safe_numeric(perf["minutes_est"])
    perf["appearances_est"] = safe_numeric(perf["appearances_est"])
    perf["performance_year"] = perf.apply(season_anchor_year, axis=1)
    perf["minutes_per_appearance"] = perf["minutes_est"] / perf["appearances_est"].replace(0, np.nan)
    perf = perf.sort_values(["player_id", "performance_year", "tm_season_id"], na_position="last")
    perf = perf.drop_duplicates(subset=["player_id", "performance_year"], keep="last")
    return perf


def build_core_dataset(profiles: pd.DataFrame, market_values: pd.DataFrame) -> pd.DataFrame:
    core = market_values.merge(
        profiles[
            [
                "player_id",
                "player_name",
                "full_name",
                "birth_date",
                "nationality",
                "position_group",
                "position_detail",
                "is_brazilian",
            ]
        ],
        on="player_id",
        how="left",
    )
    age_days = (core["valuation_date"] - core["birth_date"]).dt.days
    core["age_years"] = age_days / 365.25
    core["log_market_value"] = np.log1p(core["market_value_eur"])
    core = core.sort_values(["player_id", "valuation_date"]).reset_index(drop=True)
    core["n_valuations_total"] = core.groupby("player_id")["player_id"].transform("size")
    core["n_valuations_so_far"] = core.groupby("player_id").cumcount() + 1
    core["first_valuation_date"] = core.groupby("player_id")["valuation_date"].transform("min")
    core["time_since_first_valuation_days"] = (core["valuation_date"] - core["first_valuation_date"]).dt.days
    core["career_year"] = core["time_since_first_valuation_days"] / 365.25
    core["meets_min_3_vals"] = core["n_valuations_total"] >= 3
    core["meets_min_4_vals"] = core["n_valuations_total"] >= 4
    return core


def merge_performance_features(core: pd.DataFrame, perf: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "player_id",
        "performance_year",
        "season_label",
        "minutes_est",
        "appearances_est",
        "minutes_per_appearance",
    ]
    perf_lookup = perf[columns].sort_values(["player_id", "performance_year"]).copy()
    merged_rows = []
    for player_id, core_player in core.groupby("player_id", sort=False):
        perf_player = perf_lookup.loc[perf_lookup["player_id"] == player_id]
        core_player = core_player.copy()
        core_player["performance_year_ref"] = np.nan
        core_player["season_label_ref"] = pd.NA
        core_player["performance_gap_years"] = np.nan
        core_player["minutes_est"] = np.nan
        core_player["appearances_est"] = np.nan
        core_player["minutes_per_appearance"] = np.nan
        if perf_player.empty:
            merged_rows.append(core_player)
            continue
        perf_years = perf_player["performance_year"].to_numpy(dtype=float)
        valuation_years = core_player["valuation_date"].dt.year.to_numpy(dtype=float)
        positions = np.searchsorted(perf_years, valuation_years, side="right") - 1
        valid = positions >= 0
        if valid.any():
            chosen = perf_player.iloc[positions[valid]].reset_index(drop=True)
            idx = core_player.index[valid]
            core_player.loc[idx, "performance_year_ref"] = chosen["performance_year"].to_numpy()
            core_player.loc[idx, "season_label_ref"] = chosen["season_label"].astype("string").to_numpy()
            core_player.loc[idx, "performance_gap_years"] = valuation_years[valid] - chosen["performance_year"].to_numpy()
            core_player.loc[idx, "minutes_est"] = chosen["minutes_est"].to_numpy()
            core_player.loc[idx, "appearances_est"] = chosen["appearances_est"].to_numpy()
            core_player.loc[idx, "minutes_per_appearance"] = chosen["minutes_per_appearance"].to_numpy()
        merged_rows.append(core_player)
    return pd.concat(merged_rows, ignore_index=True)


def aggregate_injuries_for_player(core_player: pd.DataFrame, injuries_player: pd.DataFrame) -> pd.DataFrame:
    result = core_player.copy()
    result["injury_count_last_365"] = 0
    result["days_injured_last_365"] = 0.0
    result["games_missed_last_365"] = 0.0
    result["injury_recent"] = False
    result["injury_severe_last_365"] = False
    if injuries_player.empty:
        return result

    injuries_player = injuries_player.copy().sort_values("from_date")
    for idx, valuation_date in result["valuation_date"].items():
        if pd.isna(valuation_date):
            continue
        window_start = valuation_date - pd.Timedelta(days=365)
        recent_start = valuation_date - pd.Timedelta(days=180)
        overlapping = injuries_player.loc[
            (injuries_player["from_date"] <= valuation_date) & (injuries_player["to_date_filled"] >= window_start)
        ]
        if overlapping.empty:
            continue
        days_total = 0.0
        for _, injury_row in overlapping.iterrows():
            overlap_start = max(injury_row["from_date"], window_start)
            overlap_end = min(injury_row["to_date_filled"], valuation_date)
            overlap_days = max(0, (overlap_end - overlap_start).days + 1)
            days_total += overlap_days
        result.at[idx, "injury_count_last_365"] = int(len(overlapping))
        result.at[idx, "days_injured_last_365"] = float(days_total)
        result.at[idx, "games_missed_last_365"] = float(overlapping["games_missed"].fillna(0).sum())
        result.at[idx, "injury_recent"] = bool((overlapping["to_date_filled"] >= recent_start).any())
        result.at[idx, "injury_severe_last_365"] = bool(overlapping["severe_injury"].any())
    return result


def add_injury_features(core: pd.DataFrame, injuries: pd.DataFrame) -> pd.DataFrame:
    rows = []
    injuries_groups = {player_id: group for player_id, group in injuries.groupby("player_id", sort=False)}
    for player_id, core_player in core.groupby("player_id", sort=False):
        rows.append(aggregate_injuries_for_player(core_player, injuries_groups.get(player_id, injuries.iloc[0:0])))
    return pd.concat(rows, ignore_index=True)


def aggregate_transfers_for_player(core_player: pd.DataFrame, transfers_player: pd.DataFrame) -> pd.DataFrame:
    result = core_player.copy()
    result["transfer_count_career"] = 0
    result["transfer_recent"] = False
    result["international_transfer_recent"] = False
    result["competition_change_recent"] = False
    result["days_since_last_transfer"] = np.nan
    result["last_transfer_fee_eur"] = np.nan
    result["last_transfer_market_value_eur"] = np.nan
    if transfers_player.empty:
        return result

    transfers_player = transfers_player.copy().sort_values("transfer_date")
    for idx, valuation_date in result["valuation_date"].items():
        if pd.isna(valuation_date):
            continue
        prior = transfers_player.loc[transfers_player["transfer_date"] <= valuation_date]
        if prior.empty:
            continue
        last_transfer = prior.iloc[-1]
        recent_cutoff = valuation_date - pd.Timedelta(days=365)
        recent = prior.loc[prior["transfer_date"] >= recent_cutoff]
        result.at[idx, "transfer_count_career"] = int(len(prior))
        result.at[idx, "transfer_recent"] = bool(not recent.empty)
        result.at[idx, "international_transfer_recent"] = bool(recent["international_change"].any())
        result.at[idx, "competition_change_recent"] = bool(recent["competition_change"].any())
        result.at[idx, "days_since_last_transfer"] = float((valuation_date - last_transfer["transfer_date"]).days)
        result.at[idx, "last_transfer_fee_eur"] = last_transfer["transfer_fee_eur"]
        result.at[idx, "last_transfer_market_value_eur"] = last_transfer["market_value_eur"]
    return result


def add_transfer_features(core: pd.DataFrame, transfers: pd.DataFrame) -> pd.DataFrame:
    rows = []
    transfer_groups = {player_id: group for player_id, group in transfers.groupby("player_id", sort=False)}
    for player_id, core_player in core.groupby("player_id", sort=False):
        rows.append(aggregate_transfers_for_player(core_player, transfer_groups.get(player_id, transfers.iloc[0:0])))
    return pd.concat(rows, ignore_index=True)


def build_quality_summary(
    raw_datasets: dict[str, pd.DataFrame],
    cleaned: dict[str, pd.DataFrame],
    analytical: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    quality_rows = []
    missing_frames = []
    for name, raw_df in raw_datasets.items():
        clean_df = cleaned[name]
        quality_rows.append(
            {
                "dataset": name,
                "raw_rows": int(len(raw_df)),
                "clean_rows": int(len(clean_df)),
                "rows_removed": int(len(raw_df) - len(clean_df)),
                "unique_players_clean": int(clean_df["player_id"].nunique()) if "player_id" in clean_df.columns else np.nan,
            }
        )
        missing_frames.append(missing_summary(clean_df, name))
    quality_rows.append(
        {
            "dataset": "analytical_dataset",
            "raw_rows": int(len(cleaned["career_arcs_base"])),
            "clean_rows": int(len(analytical)),
            "rows_removed": int(len(cleaned["career_arcs_base"]) - len(analytical)),
            "unique_players_clean": int(analytical["player_id"].nunique()),
        }
    )
    missing_frames.append(missing_summary(analytical, "analytical_dataset"))
    return pd.DataFrame(quality_rows), pd.concat(missing_frames, ignore_index=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Limpa a base e produz a tabela analítica longitudinal.")
    parser.add_argument("--input-dir", default=str(RAW_DATA_DIR))
    parser.add_argument("--output-dir", default=str(RESULTS_DIR))
    args = parser.parse_args()

    output_dir = ensure_dir(args.output_dir)
    clean_dir = ensure_dir(output_dir / "cleaned")

    raw = load_raw_datasets(args.input_dir)

    profiles = clean_profiles(raw["profiles"])
    market_values = clean_market_values(raw["market_values"])
    injuries = clean_injuries(raw["injuries"])
    transfers = clean_transfers(raw["transfers"])
    performance = clean_performance(raw["performance_summaries"])
    squad_membership = raw["squad_membership"].drop_duplicates().copy()
    squad_membership["display_year"] = safe_numeric(squad_membership["display_year"])
    squad_membership["tm_season_id"] = safe_numeric(squad_membership["tm_season_id"])
    core_seed = build_core_dataset(profiles, market_values)
    core_seed = merge_performance_features(core_seed, performance)
    core_seed = add_injury_features(core_seed, injuries)
    core_seed = add_transfer_features(core_seed, transfers)
    analytical = core_seed.loc[
        core_seed["age_years"].between(14, 45, inclusive="both")
        & core_seed["position_group"].notna()
        & core_seed["valuation_date"].notna()
        & core_seed["market_value_eur"].notna()
    ].copy()

    cleaned = {
        "profiles": profiles,
        "market_values": market_values,
        "injuries": injuries,
        "transfers": transfers,
        "performance_summaries": performance,
        "squad_membership": squad_membership,
        "career_arcs_base": core_seed,
    }
    quality_summary, missing = build_quality_summary(raw, cleaned, analytical)

    profiles.to_csv(clean_dir / "profiles_clean.csv", index=False)
    market_values.to_csv(clean_dir / "market_values_clean.csv", index=False)
    injuries.to_csv(clean_dir / "injuries_clean.csv", index=False)
    transfers.to_csv(clean_dir / "transfers_clean.csv", index=False)
    performance.to_csv(clean_dir / "performance_clean.csv", index=False)
    squad_membership.to_csv(clean_dir / "squad_membership_clean.csv", index=False)
    core_seed.to_csv(clean_dir / "career_arcs_base_enriched.csv", index=False)
    analytical.to_csv(output_dir / "analytical_dataset.csv", index=False)
    round_frame(quality_summary).to_csv(output_dir / "data_quality_summary.csv", index=False)
    round_frame(missing).to_csv(output_dir / "missing_summary.csv", index=False)

    summary_payload = {
        "input_dir": str(Path(args.input_dir).resolve()),
        "output_dir": str(output_dir.resolve()),
        "analytical_rows": int(len(analytical)),
        "analytical_players": int(analytical["player_id"].nunique()),
        "players_with_3_vals": int(analytical.loc[analytical["meets_min_3_vals"], "player_id"].nunique()),
        "players_with_4_vals": int(analytical.loc[analytical["meets_min_4_vals"], "player_id"].nunique()),
        "age_min": float(analytical["age_years"].min()),
        "age_max": float(analytical["age_years"].max()),
        "market_value_zero_share": float((analytical["market_value_eur"] == 0).mean()),
        "injury_recent_share": float(analytical["injury_recent"].mean()),
        "transfer_recent_share": float(analytical["transfer_recent"].mean()),
    }
    write_json(output_dir / "cleaning_summary.json", summary_payload)


if __name__ == "__main__":
    main()
