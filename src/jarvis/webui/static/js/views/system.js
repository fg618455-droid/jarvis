/* System: the readings that decide whether this machine can keep up. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { chip, clear, el, empty, meter, rows, table } from "../ui.js";

const VRAM_WARN = 0.85;
const VRAM_BAD = 0.95;

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("system.title") }),
    el("p", { text: t("system.lead") }),
    el("div", { class: "actions" }, [
      el("a", { class: "btn", href: api.exportUrl(), text: t("system.export") }),
    ]),
  ]);

  const topRow = el("div", { class: "grid" });
  const modelsCard = el("section", { class: "card" });
  const speechRow = el("div", { class: "grid-2" });
  const pathsCard = el("section", { class: "card" });
  const historyCard = el("section", { class: "card" });

  root.append(head, topRow, modelsCard, speechRow, pathsCard, historyCard);

  async function refresh() {
    const [system, turns] = await Promise.all([api.system(), api.turns(50)]);
    paintTop(topRow, system);
    paintModels(modelsCard, system);
    paintSpeech(speechRow, system);
    paintPaths(pathsCard, system);
    paintHistory(historyCard, turns.turns || []);
  }

  await refresh();
  const timer = setInterval(() => refresh().catch(() => {}), 5000);
  return () => clearInterval(timer);
}

function paintTop(container, system) {
  clear(container);

  const gpu = system.gpu;
  const used = gpu?.used_mb;
  const total = gpu?.total_mb;
  const fraction = used && total ? used / total : 0;
  const tone = fraction >= VRAM_BAD ? "bad" : fraction >= VRAM_WARN ? "warn" : null;

  container.append(
    el("section", { class: "card" }, [
      el("header", {}, [
        el("h2", { text: t("system.gpu") }),
        el("span", { class: "aside", text: gpu?.name || t("common.unknown") }),
      ]),
      el("div", { class: "reading" }, [
        el("div", { class: "headline" }, [
          el("span", {
            class: "value",
            text: used && total ? `${fmt.megabytes(used)} / ${fmt.megabytes(total)}` : "—",
          }),
          tone ? chip(fmt.percent(fraction * 100), tone) : null,
        ]),
        meter(fraction, tone),
        el("div", { class: "node-meta" }, [
          gpu?.temperature_c ? el("span", { text: `${t("system.temperature")} ${gpu.temperature_c} °C` }) : null,
          gpu?.utilisation_percent !== undefined
            ? el("span", { text: `${t("system.utilisation")} ${gpu.utilisation_percent} %` })
            : null,
        ]),
      ]),
    ]),

    el("section", { class: "card" }, [
      el("header", {}, [el("h2", { text: t("system.process") })]),
      rows([
        ["PID", String(system.process?.pid ?? "—")],
        ["RAM", fmt.bytes(system.process?.rss_bytes)],
        ["Threads", String(system.process?.threads ?? "—")],
        ["Python", system.process?.python],
        ["Platform", system.process?.platform],
      ]),
    ]),

    el("section", { class: "card" }, [
      el("header", {}, [el("h2", { text: t("system.environment") })]),
      rows(
        Object.entries(system.ollama_environment || {}).map(([key, value]) => [
          key.replace("OLLAMA_", ""),
          value || t("system.notSet"),
        ]),
      ),
    ]),
  );
}

function paintModels(container, system) {
  clear(container);
  container.append(
    el("header", {}, [
      el("h2", { text: t("system.models") }),
    ]),
  );

  const effective = system.models?.effective || {};
  const effectiveRows = ["fast", "chat", "private"]
    .filter((tier) => effective[tier])
    .map((tier) => ({ tier, ...effective[tier] }));
  container.append(el("section", { class: "model-block effective-models" }, [
    el("h3", { text: t("system.effectiveRoutes") }),
    table(
      [
        { label: t("system.tier"), render: (entry) => entry.tier.toUpperCase() },
        { label: t("system.model"), render: (entry) => entry.model },
        { label: t("system.provider"), render: (entry) => entry.provider },
        { label: t("system.location"), render: (entry) => chip(t(`system.${entry.location}`)) },
      ],
      effectiveRows,
    ),
  ]));

  const local = system.models?.local || {};
  const localRoles = [
    ["fast_fallback", t("system.fastFallback")],
    ["chat_fallback", t("system.chatFallback")],
    ["private", t("system.privateModel")],
    ["embedding", t("system.embeddingModel")],
  ];
  const localRows = localRoles
    .filter(([key]) => local[key])
    .map(([key, role]) => ({ role, ...local[key] }));
  container.append(el("section", { class: "model-block local-models" }, [
    el("h3", { text: t("system.localModels") }),
    table(
      [
        { label: t("system.role"), render: (entry) => entry.role },
        { label: t("system.model"), render: (entry) => entry.model },
        { label: t("system.provider"), render: (entry) => entry.provider },
      ],
      localRows,
    ),
  ]));

  const resident = system.models?.resident || system.models?.loaded || [];
  const gpu = system.gpu || {};
  const localGpu = gpu.used_mb !== undefined && gpu.total_mb !== undefined
    ? `${fmt.megabytes(gpu.used_mb)} / ${fmt.megabytes(gpu.total_mb)}`
    : gpu.total_mb !== undefined ? fmt.megabytes(gpu.total_mb) : "—";
  container.append(el("section", { class: "model-block resident-models" }, [
    el("div", { class: "model-block-head" }, [
      el("h3", { text: t("system.residentModels") }),
      el("span", { class: "aside", text: `${t("sidebar.localGpu")}: ${localGpu}` }),
    ]),
    resident.length ? table(
      [
        { label: t("common.name"), render: (model) => model.name },
        { label: "size", numeric: true, render: (model) => model.size },
        { label: "processor", render: (model) => model.processor },
        { label: "context", numeric: true, render: (model) => model.context },
        { label: "until", numeric: true, render: (model) => model.until },
      ],
      resident,
    ) : el("span", { class: "muted", text: t("system.noModels") }),
  ]));
}

function paintSpeech(container, system) {
  clear(container);
  const input = system.speech_recognition || {};
  const output = system.speech_output || {};

  container.append(
    el("section", { class: "card" }, [
      el("header", {}, [el("h2", { text: t("system.speechIn") })]),
      rows([
        ["model", fmt.truncate(input.model, 60)],
        ["backend", input.backend],
        ["device", input.device],
        ["compute", input.compute_type],
        ["vad", input.vad ? t("common.yes") : t("common.no")],
        ["language", input.language],
        ["min language p", fmt.number(input.min_language_probability, 2)],
      ]),
    ]),

    el("section", { class: "card" }, [
      el("header", {}, [el("h2", { text: t("system.speechOut") })]),
      rows([
        ["engine", output.engine],
        ["enabled", output.enabled ? t("common.yes") : t("common.no")],
        ["cloud chain", (output.cloud_providers || [])
          .filter((provider) => provider.enabled)
          .map((provider) => `${provider.name}: ${provider.model}`)
          .join(", ") || "—"],
        ["local fallback", output.local_fallback_engine],
        ["voice", fmt.truncate(output.model, 60)],
        ["language", output.language || t("common.unknown")],
      ]),
    ]),
  );
}

function paintPaths(container, system) {
  clear(container);
  container.append(el("header", {}, [el("h2", { text: t("system.paths") })]));

  const list = el("div", { class: "path-list" });
  for (const entry of system.paths || []) {
    list.append(
      el("div", {}, [
        el("div", {}, [
          el("strong", { text: entry.label }),
          entry.exists ? null : chip("missing", "warn"),
          entry.size_bytes ? el("span", { class: "muted", text: ` ${fmt.bytes(entry.size_bytes)}` }) : null,
          entry.disk_free_bytes
            ? el("span", {
                class: "muted",
                text: ` · ${fmt.bytes(entry.disk_free_bytes)} ${t("system.free")}`,
              })
            : null,
        ]),
        el("div", { class: "path", text: entry.path }),
      ]),
    );
  }
  container.append(list);
}

function paintHistory(container, turns) {
  clear(container);
  container.append(
    el("header", {}, [
      el("h2", { text: t("system.latency") }),
      el("span", { class: "aside", text: `${turns.length}` }),
    ]),
  );

  if (!turns.length) {
    container.append(empty(t("deck.noTurns")));
    return;
  }

  const stageNames = [];
  for (const turn of turns) {
    for (const stage of turn.stages || []) {
      if (!stageNames.includes(stage.name)) stageNames.push(stage.name);
    }
  }

  const columns = [
    { label: "", numeric: true, render: (turn) => fmt.time(turn.started_at) },
    { label: t("common.total"), numeric: true, render: (turn) => fmt.ms(turn.total_ms) },
    ...stageNames.map((name) => ({
      label: name,
      numeric: true,
      render: (turn) => {
        const total = (turn.stages || [])
          .filter((stage) => stage.name === name)
          .reduce((sum, stage) => sum + stage.duration_ms, 0);
        return total ? fmt.ms(total) : "—";
      },
    })),
  ];

  container.append(el("div", { class: "scroll" }, [table(columns, [...turns].reverse())]));
}
