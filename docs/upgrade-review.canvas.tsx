import {
  BarChart,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Grid,
  H1,
  H2,
  Pill,
  Row,
  Stack,
  Stat,
  Table,
  Text,
  useHostTheme,
} from "cursor/canvas";

/** Capability scores 0–5 from docs + local code review · Sep 24 2026 */
const CAPABILITY = {
  categories: [
    "Coverage",
    "Reinstall",
    "Safety",
    "Snapshot/diff",
    "Eng quality",
    "Distribution",
  ],
  series: [
    {
      name: "This repo (v1.2)",
      data: [2, 3, 5, 5, 5, 2],
      tone: "info" as const,
    },
    {
      name: "winget-cli",
      data: [3, 5, 4, 1, 5, 5],
      tone: "success" as const,
    },
    {
      name: "AppList",
      data: [5, 5, 3, 3, 3, 2],
      tone: "warning" as const,
    },
    {
      name: "swinv",
      data: [5, 1, 4, 2, 4, 2],
      tone: "neutral" as const,
    },
  ],
};

const COMPETITORS = [
  {
    project: "This repo",
    stars: "0",
    lang: "Python",
    sources: "HKLM 32/64 + HKCU Uninstall",
    reinstall: "winget import JSON + unmatched checklist",
    writes: "Never",
    bestFor: "Safe audit, diffs, reinstall companion",
  },
  {
    project: "microsoft/winget-cli",
    stars: "26.4k",
    lang: "C++",
    sources: "ARP matched to winget catalog",
    reinstall: "export → import (gold standard)",
    writes: "Installs / upgrades packages",
    bestFor: "Post-wipe restore of catalog apps",
  },
  {
    project: "SysAdminDoc/AppList",
    stars: "2",
    lang: "Python / PyQt6",
    sources: "Registry + Store + winget + choco/scoop/pip…",
    reinstall: "winget JSON + install.ps1 + bundle",
    writes: "Optional removal scripts (dry-run default)",
    bestFor: "Migration / rebuild planning",
  },
  {
    project: "chaugan/swinv",
    stars: "1",
    lang: "Go",
    sources: "Registry + Appx + servicing (+ optional full FS)",
    reinstall: "Inventory only",
    writes: "Read-only by default",
    bestFor: "Maximum discovery depth",
  },
  {
    project: "NirSoft UninstallView",
    stars: "n/a",
    lang: "Native util",
    sources: "Uninstall keys (+ leftovers)",
    reinstall: "Not the goal",
    writes: "Can uninstall",
    bestFor: "Interactive cleanup",
  },
];

const ARCHITECTURE = [
  {
    layer: "Collector",
    today: "windows_registry only",
    gap: "No Appx / package-manager collectors",
  },
  {
    layer: "Normalize",
    today: "Dedupe key includes version; filters updates/system",
    gap: "No source tags; blank install_location weakens identity",
  },
  {
    layer: "Report",
    today: "v1.1 envelope + privacy-aware scan metadata",
    gap: "No checked-in JSON Schema artifact for CI validation",
  },
  {
    layer: "Diff",
    today: "Identity = name + publisher + location (version excluded)",
    gap: "Publisher rename / path move shows as remove + add",
  },
  {
    layer: "Export",
    today: "table / JSON / CSV / winget import + unmatched checklist",
    gap: "Name-only winget matching; no HTML summary",
  },
  {
    layer: "Ops",
    today: "PS runner: JSON/CSV, latest/previous, auto-diff, winget pair",
    gap: "No Task Scheduler template; no PyPI release yet",
  },
];

type Status = "done" | "next" | "planned";

const UPGRADES: {
  id: string;
  title: string;
  status: Status;
  why: string;
  work: string;
  effort: string;
}[] = [
  {
    id: "P0",
    title: "Winget bridge",
    status: "done",
    why: "Closed the biggest gap vs winget / AppList for reinstall after an OS wipe.",
    work: "Shipped in v1.2.0: --format winget, --winget-list, --include-versions, *.unmatched.md checklist.",
    effort: "M",
  },
  {
    id: "P1",
    title: "Appx / MSIX collector",
    status: "next",
    why: "Store apps never appear in Uninstall keys — the coverage hole every competitor calls out.",
    work: "Read-only Appx enumeration → SoftwareEntry with source=appx; merge policy in prepare.",
    effort: "M",
  },
  {
    id: "P2",
    title: "Collector protocol + schema 2.0",
    status: "planned",
    why: "Scale to multiple sources cleanly and enable CI contract tests.",
    work: "Collector protocol; source field on entries; JSON Schema validated in CI; golden fixtures.",
    effort: "M–L",
  },
  {
    id: "P3",
    title: "Stronger identity and matching",
    status: "planned",
    why: "Reduce false remove+add in diffs and improve winget match rate beyond display names.",
    work: "ProductCode / subkey stem in identity; publisher-aware winget matching; diff reason codes.",
    effort: "S–M",
  },
  {
    id: "P4",
    title: "Distribution",
    status: "planned",
    why: "Discovery is near zero today.",
    work: "PyPI package, GitHub topics, optional winget manifest for the tool itself.",
    effort: "S",
  },
];

const STATUS_TONE: Record<Status, "success" | "warning" | "neutral"> = {
  done: "success",
  next: "warning",
  planned: "neutral",
};

const DONT = [
  "Don't query Win32_Product — already correctly avoided",
  "Don't ship silent uninstall or auto-import — keep the read-only safety brand",
  "Don't try to out-scope swinv's full filesystem scan — stay local-first and fast",
  "Don't commit personal inventory reports to the public repo",
];

export default function RepoUpgradeReview() {
  const theme = useHostTheme();

  return (
    <Stack gap={28} style={{ padding: 24, maxWidth: 1120 }}>
      <Stack gap={8}>
        <H1>Installed Software Inventory — Upgrade Review</H1>
        <Text tone="secondary">
          Architecture and competitive review from local source + GitHub
          metadata · updated 24 Sep 2026 after the v1.2.0 winget bridge.
        </Text>
        <Row gap={8} style={{ flexWrap: "wrap" }}>
          <Pill tone="info">v1.2.0</Pill>
          <Pill tone="success">66 tests · 15 harness cases</Pill>
          <Pill tone="neutral">stdlib only · 0 runtime deps</Pill>
          <Pill tone="warning">Coverage: Uninstall keys only</Pill>
        </Row>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value="5/5" label="Safety score" tone="success" />
        <Stat value="5/5" label="Snapshot / diff score" tone="success" />
        <Stat value="3/5" label="Reinstall score (was 1/5)" tone="info" />
        <Stat value="2/5" label="Coverage score" tone="warning" />
      </Grid>

      <Callout tone="info" title="Core finding">
        Engineering quality is already strong (stdlib pipeline, envelope schema,
        identity-aware diff, CI + process harness). The v1.2.0 winget bridge
        turns the tool from an auditor into an auditor plus reinstall
        companion. The remaining gap is coverage: Store/MSIX apps are invisible
        to Uninstall keys, so P1 is next.
      </Callout>

      <Stack gap={12}>
        <H2>Capability scores (0–5, higher is better)</H2>
        <Text size="small" tone="secondary">
          Source: local architecture review + public READMEs / GitHub API · Sep
          2026
        </Text>
        <BarChart
          categories={CAPABILITY.categories}
          series={CAPABILITY.series}
          height={260}
        />
      </Stack>

      <Stack gap={12}>
        <H2>Competitive landscape</H2>
        <Table
          headers={[
            "Project",
            "Stars",
            "Lang",
            "Sources",
            "Reinstall",
            "System writes",
            "Best for",
          ]}
          rows={COMPETITORS.map((c) => [
            c.project,
            c.stars,
            c.lang,
            c.sources,
            c.reinstall,
            c.writes,
            c.bestFor,
          ])}
          rowTone={COMPETITORS.map((_, i) =>
            i === 0 ? ("info" as const) : undefined,
          )}
        />
        <Text size="small" tone="secondary">
          GitHub stars fetched Sep 24 2026. Stars are not fit — niche audit
          tools stay small.
        </Text>
      </Stack>

      <Divider />

      <Stack gap={12}>
        <H2>What this repo already nails</H2>
        <Grid columns={2} gap={12}>
          <Card>
            <CardHeader>Safety contract</CardHeader>
            <CardBody>
              <Text>
                Read-only Registry, no network for scans, no Win32_Product,
                SOFTWARE_INVENTORY_SKIP_LIVE_SCAN enforced in collector + CI.
                The winget bridge only reads winget list; it never imports.
              </Text>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>Snapshot / diff design</CardHeader>
            <CardBody>
              <Text>
                Dedupe includes version (co-installed versions stay distinct);
                diff identity excludes version (upgrades show as Changed).
              </Text>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>Testability</CardHeader>
            <CardBody>
              <Text>
                66 unit tests + 15 CLI process harness cases against fixtures;
                --from-json and --winget-list work offline; Windows CI matrix
                3.10–3.13.
              </Text>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>Reinstall workflow</CardHeader>
            <CardBody>
              <Text>
                winget import JSON for catalog apps plus a Markdown checklist
                for everything winget cannot match.
              </Text>
            </CardBody>
          </Card>
        </Grid>
      </Stack>

      <Stack gap={12}>
        <H2>Architecture vs next level</H2>
        <Table
          headers={["Layer", "Today", "Gap to close"]}
          rows={ARCHITECTURE.map((a) => [a.layer, a.today, a.gap])}
        />
      </Stack>

      <Stack gap={12}>
        <H2>Upgrade roadmap</H2>
        {UPGRADES.map((u) => (
          <Card key={u.id}>
            <CardHeader
              trailing={
                <Row gap={6}>
                  <Pill tone={STATUS_TONE[u.status]}>{u.status}</Pill>
                  <Pill tone="info">effort {u.effort}</Pill>
                </Row>
              }
            >
              {u.id} — {u.title}
            </CardHeader>
            <CardBody>
              <Stack gap={6}>
                <Text>{u.why}</Text>
                <Text size="small" tone="secondary">
                  {u.work}
                </Text>
              </Stack>
            </CardBody>
          </Card>
        ))}
      </Stack>

      <Stack gap={12}>
        <H2>Explicit non-goals</H2>
        <Stack gap={6}>
          {DONT.map((line) => (
            <Text key={line}>• {line}</Text>
          ))}
        </Stack>
      </Stack>

      <Callout tone="success" title="Recommended next build">
        P1: a read-only Appx/MSIX collector with source tags, so Store apps
        show up in snapshots, diffs, and the unmatched reinstall checklist.
      </Callout>

      <Text size="small" style={{ color: theme.text.tertiary }}>
        Evidence: local pytest 66/66, CLI harness 15/15, GitHub API for
        competitor star counts.
      </Text>
    </Stack>
  );
}
