/* Formatting. Every reading the interface shows passes through here so a
   duration, a size, and a timestamp look the same wherever they appear. */

import { locale } from "./i18n.js";

export function ms(value) {
  if (value === null || value === undefined) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toLocaleString(locale(), {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} s`;
}

export function seconds(value) {
  if (value === null || value === undefined) return "—";
  const total = Math.floor(value);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function bytes(value) {
  if (!value && value !== 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let unit = 0;
  while (n >= 1024 && unit < units.length - 1) {
    n /= 1024;
    unit += 1;
  }
  return `${n.toLocaleString(locale(), { maximumFractionDigits: n < 10 ? 1 : 0 })} ${units[unit]}`;
}

export function megabytes(value) {
  if (!value && value !== 0) return "—";
  return value >= 1024
    ? `${(value / 1024).toLocaleString(locale(), { maximumFractionDigits: 2 })} GB`
    : `${Math.round(value)} MB`;
}

export function number(value, digits = 0) {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString(locale(), {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function percent(value) {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value)} %`;
}

export function time(epochSeconds) {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleTimeString(locale(), {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function dateTime(epochSeconds) {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString(locale(), {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function date(isoDate) {
  if (!isoDate) return "—";
  const parsed = new Date(isoDate);
  if (Number.isNaN(parsed.getTime())) return isoDate;
  return parsed.toLocaleDateString(locale(), { dateStyle: "medium" });
}

/* "3 min ago", without pulling in a library: the interface only ever needs
   coarse recency, never a precise interval. */
export function ago(epochSeconds, t) {
  if (!epochSeconds) return "—";
  const delta = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (delta < 60) return t("ago.now");
  if (delta < 3600) return t("ago.minutes", { n: Math.floor(delta / 60) });
  if (delta < 86400) return t("ago.hours", { n: Math.floor(delta / 3600) });
  return t("ago.days", { n: Math.floor(delta / 86400) });
}

export function truncate(text, max = 120) {
  const value = String(text ?? "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}
