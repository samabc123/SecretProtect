<template>
  <AppLayout>
    <div class="overview-page">
      <LoadingState :loading="loading" :error="error" @retry="load" />

      <template v-if="!loading && !error">
        <section class="overview-hero" aria-labelledby="overview-title">
          <div class="hero-copy">
            <div class="hero-meta">
              <span class="hero-kicker">Security operations / evidence desk</span>
              <span class="hero-status"><i />授权范围内运行</span>
            </div>
            <h2 id="overview-title">云上凭据泄露自动化检测平台</h2>
            <p>
              把采集来源、检测候选、统一凭据实体和处置队列放在同一条证据链上，
              让当前覆盖范围、风险状态和下一步动作可以快速核对。
            </p>
            <div class="hero-actions">
              <el-button type="primary" :loading="running" :icon="VideoPlay" @click="collect">
                立即采集
              </el-button>
              <el-button :icon="Download" @click="exportData">导出报告</el-button>
              <el-button text type="primary" :icon="ArrowRight" @click="router.push('/events')">
                查看事件列表
              </el-button>
            </div>
          </div>
          <div class="hero-visual" aria-hidden="true">
            <SecretRadarLogo class="hero-radar-logo" :size="compactHero ? 56 : 176" :animated="true" variant="dark" />
          </div>
        </section>

        <section class="pipeline-grid" aria-label="检测链路">
          <article v-for="stage in pipeline" :key="stage.title" class="pipeline-stage">
            <div class="stage-topline">
              <span class="stage-index">{{ stage.index }}</span>
              <el-icon class="stage-icon" :style="{ color: stage.color }"><component :is="stage.icon" /></el-icon>
            </div>
            <h3>{{ stage.title }}</h3>
            <p>{{ stage.description }}</p>
            <strong>{{ stage.stat }}</strong>
          </article>
        </section>

        <div class="overview-grid detail-grid">
          <el-card shadow="never" class="overview-panel">
            <template #header>
              <div class="panel-heading">
                <div>
                  <span class="panel-kicker">Coverage</span>
                  <h3>渠道覆盖</h3>
                </div>
                <span class="panel-summary">{{ channelTypeCount }} 种 / {{ channelPlatformCount }} 平台</span>
                <el-button link type="primary" :icon="ArrowRight" @click="router.push('/channels')">
                  查看矩阵
                </el-button>
              </div>
            </template>
            <div v-if="channelBars.length" class="coverage-list">
              <div v-for="item in channelBars" :key="item.channel" class="coverage-row">
                <div class="coverage-label">
                  <span class="coverage-dot" :style="{ background: item.color }" />
                  <span>{{ item.label }}</span>
                  <small>
                    {{ item.platformCount > 0 ? `${item.platformCount} 平台` : "平台数未提供" }}
                    / {{ item.objects }} 对象
                  </small>
                </div>
                <div class="coverage-track" aria-hidden="true">
                  <span :style="{ width: `${item.percent}%`, background: item.color }" />
                </div>
              </div>
            </div>
            <el-empty v-else description="暂无渠道覆盖数据" />
            <div v-if="platformCoverageRows.length" class="platform-tags">
              <el-tag
                v-for="item in platformCoverageRows"
                :key="`${item.channel}-${item.platform}`"
                effect="plain"
              >
                {{ channelLabel(item.channel) }} · {{ item.platform }} · {{ item.count }}
              </el-tag>
            </div>
          </el-card>

          <el-card shadow="never" class="overview-panel type-panel">
            <template #header>
              <div class="panel-heading">
                <div>
                  <span class="panel-kicker">CREDENTIAL TYPES</span>
                  <h3>凭据类型</h3>
                </div>
                <span class="panel-summary">
                  {{ credentialTypeTotal }} 种类型 · {{ allCredentialsTotal }} 个去重凭据实体
                </span>
              </div>
            </template>
            <div v-if="credentialDonutSegments.length" class="type-distribution">
              <div
                class="type-donut-wrap"
                :class="{ 'is-hovered': activeCredentialType }"
                @mouseleave="activeCredentialType = null"
              >
                <svg
                  class="type-donut"
                  :class="{ 'is-dense': credentialDonutSegments.length > 24 }"
                  viewBox="0 0 220 220"
                  role="img"
                  :aria-label="`凭据类型分布环形图，共 ${credentialTypeTotal} 种类型`"
                >
                  <path
                    v-for="item in credentialDonutSegments"
                    :key="item.type"
                    class="type-donut-segment"
                    :class="{ 'is-active': activeCredentialType === item.type }"
                    :d="item.path"
                    :fill="item.color"
                    :stroke="item.color"
                    :transform="activeCredentialType === item.type ? item.activeTransform : item.transform"
                    @mouseenter="activeCredentialType = item.type"
                    @focus="activeCredentialType = item.type"
                    tabindex="0"
                  />
                </svg>
                <div v-if="activeCredentialSegment" class="type-donut-tooltip" role="status">
                  <span>{{ activeCredentialSegment.label }}</span>
                  <strong>{{ activeCredentialSegment.count }}</strong>
                  <small>个去重凭据实体</small>
                </div>
              </div>
              <div class="type-legend">
                <button
                  v-for="item in credentialDonutSegments"
                  :key="`${item.type}-legend`"
                  type="button"
                  class="type-legend-row"
                  :class="{ 'is-active': activeCredentialType === item.type }"
                  @mouseenter="activeCredentialType = item.type"
                  @focus="activeCredentialType = item.type"
                  @mouseleave="activeCredentialType = null"
                  @blur="activeCredentialType = null"
                >
                  <span
                    class="type-legend-swatch"
                    :style="{ backgroundColor: item.color }"
                  />
                  <span class="type-legend-label" :title="item.label">{{ item.label }}</span>
                  <strong>{{ item.count }}</strong>
                </button>
              </div>
            </div>
            <el-empty v-else description="当前没有凭据类型数据" />
          </el-card>
        </div>

        <section class="analysis-stack" aria-label="趋势分析">
          <el-card shadow="never" class="overview-panel trend-panel">
            <template #header>
              <div class="panel-heading trend-heading">
                <div>
                  <span class="panel-kicker">Recent activity</span>
                  <h3>近期泄露事件趋势</h3>
                </div>
                <div class="trend-legend">
                  <span class="trend-scope">全部来源</span>
                  <span><i class="trend-dot trend-dot--new" />新发现事件</span>
                  <span><i class="trend-dot trend-dot--confirmed" />已确认事件</span>
                </div>
              </div>
            </template>
            <div v-if="trendPoints.length" class="trend-chart-wrap">
              <div class="trend-y-axis" aria-hidden="true">
                <span v-for="tick in trendTicks" :key="tick">{{ tick }}</span>
              </div>
              <svg
                class="trend-chart"
                viewBox="0 0 760 300"
                role="img"
                aria-label="近期泄露事件趋势折线图"
                @mousemove="handleTrendMove"
                @mouseleave="activeTrendIndex = null"
              >
                <defs>
                  <linearGradient id="newTrendFill" x1="0" x2="0" y1="0" y2="1">
                    <stop offset="0%" stop-color="#2865e8" stop-opacity=".22" />
                    <stop offset="100%" stop-color="#2865e8" stop-opacity="0" />
                  </linearGradient>
                  <linearGradient id="confirmedTrendFill" x1="0" x2="0" y1="0" y2="1">
                    <stop offset="0%" stop-color="#20c7dc" stop-opacity=".18" />
                    <stop offset="100%" stop-color="#20c7dc" stop-opacity="0" />
                  </linearGradient>
                </defs>
                <line
                  v-for="grid in trendGridLines"
                  :key="grid.value"
                  class="trend-grid-line"
                  x1="48"
                  :y1="grid.y"
                  x2="742"
                  :y2="grid.y"
                />
                <path class="trend-area trend-area--new" :d="trendAreaPath.new" />
                <path class="trend-area trend-area--confirmed" :d="trendAreaPath.confirmed" />
                <path class="trend-line trend-line--new" :d="trendLinePath.new" />
                <path class="trend-line trend-line--confirmed" :d="trendLinePath.confirmed" />
                <line
                  v-if="activeTrendPoint"
                  class="trend-hover-line"
                  :x1="activeTrendPoint.x"
                  y1="32"
                  :x2="activeTrendPoint.x"
                  y2="244"
                />
                <g v-for="point in trendPoints" :key="point.date">
                  <circle
                    class="trend-point-hit"
                    :cx="point.x"
                    :cy="point.newY"
                    r="14"
                    tabindex="0"
                    :aria-label="`${point.label} 新发现事件 ${point.newCount}`"
                    @mouseenter="activeTrendIndex = point.index"
                    @focus="activeTrendIndex = point.index"
                  />
                  <circle class="trend-point trend-point--new" :cx="point.x" :cy="point.newY" r="4" />
                  <circle
                    class="trend-point-hit"
                    :cx="point.x"
                    :cy="point.confirmedY"
                    r="14"
                    tabindex="0"
                    :aria-label="`${point.label} 已确认事件 ${point.confirmedCount}`"
                    @mouseenter="activeTrendIndex = point.index"
                    @focus="activeTrendIndex = point.index"
                  />
                  <circle class="trend-point trend-point--confirmed" :cx="point.x" :cy="point.confirmedY" r="4" />
                  <text class="trend-x-label" :x="point.x" y="282" text-anchor="middle">{{ point.label }}</text>
                </g>
                <rect
                  class="trend-hover-zone"
                  x="48"
                  y="32"
                  width="694"
                  height="212"
                  @mousemove="handleTrendMove"
                  @mouseleave="activeTrendIndex = null"
                />
                <g v-if="activeTrendPoint" class="trend-tooltip" :transform="`translate(${activeTrendPoint.tooltipX} ${activeTrendPoint.tooltipY})`">
                  <rect width="156" height="72" rx="10" />
                  <text x="12" y="20" class="trend-tooltip-title">{{ activeTrendPoint.label }}</text>
                  <circle cx="14" cy="39" r="4" class="trend-point--new" />
                  <text x="25" y="43">新发现事件</text>
                  <text x="130" y="43" class="trend-tooltip-value">{{ activeTrendPoint.newCount }}</text>
                  <circle cx="14" cy="58" r="4" class="trend-point--confirmed" />
                  <text x="25" y="62">已确认事件</text>
                  <text x="130" y="62" class="trend-tooltip-value">{{ activeTrendPoint.confirmedCount }}</text>
                </g>
              </svg>
            </div>
            <el-empty v-else description="当前没有足够的真实事件趋势数据" />
          </el-card>
        </section>
      </template>
    </div>
  </AppLayout>
</template>

<script setup>
import { useCompactLayout } from "../composables/useCompactLayout";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import {
  ArrowRight,
  Bell,
  Connection,
  DataAnalysis,
  Download,
  Search,
  VideoPlay,
} from "@element-plus/icons-vue";
import { useRouter } from "vue-router";
import AppLayout from "../components/AppLayout.vue";
import LoadingState from "../components/LoadingState.vue";
import SecretRadarLogo from "../components/SecretRadarLogo.vue";
import * as systemApi from "../api/system";
import * as findingsApi from "../api/findings";
import * as eventsApi from "../api/events";
import * as ruleApi from "../api/rules";
import { downloadJson } from "../utils/format";

const router = useRouter();
const compactHero = useCompactLayout("(max-width: 760px)");
const overview = ref({});
const channelConnectors = ref([]);
const supportMatrix = ref([]);
const coverage = ref({});
const loading = ref(true);
const error = ref("");
const running = ref(false);
const findingSummary = ref(null);
const ruleCount = ref(null);
const trendFindings = ref([]);
const activeCredentialType = ref(null);
const activeTrendIndex = ref(null);
let overviewLoadInFlight = false;
const metrics = computed(() => overview.value.metrics || []);
const channelTypeCount = computed(() => (
  Number(coverage.value.configured_channel_type_count)
  || Number(overview.value.coverage?.configured_channel_type_count)
  || new Set((overview.value.channel_summary || []).map((item) => item.channel).filter(Boolean)).size
));
const channelPlatformCount = computed(() => (
  Number(coverage.value.configured_platform_count)
  || Number(overview.value.coverage?.configured_platform_count)
  || new Set(channelConnectors.value.map((item) => item.platform).filter(Boolean)).size
));
const ruleCredentialCount = computed(() => (
  ruleCount.value ?? "—"
));
const pendingVerificationCount = computed(() => (
  findingSummary.value?.pending_verification_count ?? "—"
));

const channelCoverageColor = "#5795c7";
const channelLabels = {
  code_hosting: "代码托管",
  package_registry: "包仓库",
  wiki: "Wiki",
  knowledge_sharing: "知识分享",
  social_public: "微博",
  container: "容器镜像",
  ai_assets: "AI 模型与数据",
  app: "应用包",
  miniprogram: "小程序",
  public_web: "公开网页",
  authorized_import: "授权离线导入",
  local_import: "本地授权载体",
  controlled_test: "受控测试载体",
  unknown: "其他渠道",
  other: "其他",
};
const coverageChannelOrder = [
  "wiki",
  "social_public",
  "code_hosting",
  "knowledge_sharing",
  "container",
  "app",
  "miniprogram",
];
const coverageChannelRanks = new Map(
  coverageChannelOrder.map((channel, index) => [channel, index]),
);
const platformChannelMatchers = [
  { channel: "code_hosting", matches: ["github（授权 api）", "gitee（授权 api）", "github", "gitee"] },
  { channel: "package_registry", matches: ["npm registry", "pypi", "crates.io", "nuget", "rubygems"] },
  { channel: "wiki", matches: ["mediawiki"] },
  { channel: "knowledge_sharing", matches: ["rss", "atom", "sitemap", "url", "rss/atom/sitemap/url", "stack overflow", "stackoverflow"] },
  { channel: "social_public", matches: ["微博（官方 oauth api）", "微博", "weibo"] },
  { channel: "container", matches: ["oci registry"] },
  { channel: "ai_assets", matches: ["hugging face hub", "huggingface"] },
  { channel: "app", matches: ["android apk"] },
  { channel: "miniprogram", matches: ["未加密 wxapkg", "wxapkg"] },
  { channel: "public_web", matches: ["rss/atom/sitemap/url", "网页"] },
];
const coveragePlatformCatalog = {
  code_hosting: ["GitHub（授权 API）", "Gitee（授权 API）"],
  package_registry: ["npm Registry", "PyPI", "crates.io", "NuGet", "RubyGems"],
  wiki: ["MediaWiki"],
  knowledge_sharing: ["RSS", "Atom", "Sitemap", "URL", "Stack Overflow"],
  social_public: ["微博（官方 OAuth API）"],
  container: ["OCI Registry"],
  ai_assets: ["Hugging Face Hub"],
  app: ["Android APK"],
  miniprogram: ["未加密 wxapkg"],
  public_web: ["RSS", "Atom", "Sitemap", "URL"],
  authorized_import: ["通用 JSONL 导出"],
  local_import: ["本地目录"],
  controlled_test: ["本地授权夹具"],
};
const channelAliases = {
  App: "app",
  应用包: "app",
  小程序: "miniprogram",
  Wiki: "wiki",
  微博: "social_public",
  "微博（官方 OAuth API）": "social_public",
  代码托管平台: "code_hosting",
  知识共享平台: "knowledge_sharing",
  知识共享: "knowledge_sharing",
  知识分享: "knowledge_sharing",
  容器镜像: "container",
};
function normalizeValue(value) {
  return String(value ?? "").trim().toLowerCase().replace(/[_-]+/g, " ");
}

function channelLabel(value) {
  const key = String(value || "").trim();
  return channelLabels[key] || key || "未知渠道";
}

function channelKey(value) {
  const key = String(value || "").trim();
  if (channelAliases[key]) return channelAliases[key];
  if (channelLabels[key]) return key;
  return Object.entries(channelLabels).find(([, label]) => label === key)?.[0] || key;
}

function channelSortRank(value) {
  return coverageChannelRanks.get(channelKey(value)) ?? coverageChannelOrder.length;
}

function compareCoverageChannels(left, right) {
  const rankDifference = channelSortRank(left.channel) - channelSortRank(right.channel);
  if (rankDifference) return rankDifference;
  return (
    (Number(right.objects) || 0) - (Number(left.objects) || 0)
    || channelLabel(left.channel).localeCompare(channelLabel(right.channel), "zh-CN")
  );
}

function platformChannel(platform) {
  const value = normalizeValue(platform);
  return platformChannelMatchers.find((item) => item.matches.some((match) => value === normalizeValue(match)))?.channel || "";
}

function platformDisplayLabel(channel, platform) {
  const value = String(platform || "").trim();
  if (value === "本地授权夹具") return "授权夹具";
  if (value === "通用 JSONL 导出") return "JSONL 导出";
  if (
    channelKey(channel) === "knowledge_sharing"
    && normalizeValue(value) !== normalizeValue("Stack Overflow")
  ) {
    return "RSS/Atom/Sitemap/URL";
  }
  return value;
}

function platformCountForChannel(channel) {
  const key = channelKey(channel);
  if (coveragePlatformCatalog[key]) return coveragePlatformCatalog[key].length;
  const summary = overview.value.platform_summary || [];
  const matched = summary.filter((item) => platformChannel(item.platform) === key);
  return new Set(matched.map((item) => item.platform)).size;
}

const configuredChannelRows = computed(() => {
  const observed = new Map();
  for (const [channel, count] of (overview.value.all_by_channel || overview.value.by_channel || [])) {
    const key = channelKey(channel);
    observed.set(key, (observed.get(key) || 0) + (Number(count) || 0));
  }
  const grouped = new Map();
  for (const row of supportMatrix.value) {
    const channel = String(row.channel_type || "").trim();
    const platform = String(row.platform || "").trim();
    if (!channel || !platform) continue;
    if (!grouped.has(channel)) grouped.set(channel, new Set());
    grouped.get(channel).add(platform);
  }
  return [...grouped.entries()]
    .map(([channel, platforms]) => ({
      channel,
      objects: observed.get(channelKey(channel)) || 0,
      platformCount: platforms.size,
    }))
    .sort(compareCoverageChannels);
});

const configuredPlatformGroups = computed(() => {
  const groups = new Map();
  for (const row of supportMatrix.value) {
    const channel = String(row.channel_type || "").trim();
    const platform = String(row.platform || "").trim();
    if (!channel || !platform) continue;
    const channelValue = channelKey(channel) || channel;
    const label = platformDisplayLabel(channel, platform);
    const key = `${channelValue}::${label}`;
    if (!groups.has(key)) {
      groups.set(key, {
        channel: channelValue,
        platform: label,
        platformCount: 0,
      });
    }
    groups.get(key).platformCount += 1;
  }
  return [...groups.values()];
});

const channelBars = computed(() => {
  const rawList = configuredChannelRows.value.length
    ? configuredChannelRows.value
    : (overview.value.all_channel_summary || overview.value.channel_summary || []).map((item) => ({
      ...item,
      platformCount: platformCountForChannel(item.channel),
    }));
  const list = [...rawList];
  list.sort((left, right) => (
    (Number(right.objects) || 0) - (Number(left.objects) || 0)
    || (Number(right.platformCount) || 0) - (Number(left.platformCount) || 0)
    || compareCoverageChannels(left, right)
  ));
  const max = Math.max(1, ...list.map((item) => Number(item.objects) || 0));
  return list.map((item) => ({
    ...item,
    label: channelLabel(item.channel),
    color: channelCoverageColor,
    percent: Math.max(5, Math.round(((Number(item.objects) || 0) / max) * 100)),
  }));
});

const platformCoverageRows = computed(() => {
  const rows = new Map();
  const observed = overview.value.platform_write_summary || overview.value.all_platform_summary || [];
  for (const item of observed) {
    const channel = String(item.channel || "").trim();
    const platform = platformDisplayLabel(channel, item.platform);
    const count = Number(item.count) || 0;
    if (!platform || count <= 0) continue;
    const channelValue = channelKey(channel) || channel;
    const key = `${channelValue}::${platform}`;
    const existing = rows.get(key);
    rows.set(key, {
      channel: channelValue,
      platform,
      count: (existing?.count || 0) + count,
      platformCount: configuredPlatformGroups.value.find(
        (group) => group.channel === channelValue && group.platform === platform,
      )?.platformCount || 0,
    });
  }
  return [...rows.values()].sort((left, right) => {
    if (left.channel === "other" && right.channel !== "other") return 1;
    if (right.channel === "other" && left.channel !== "other") return -1;
    const rankDifference = channelSortRank(left.channel) - channelSortRank(right.channel);
    if (rankDifference) return rankDifference;
    return (
      right.count - left.count
      || channelLabel(left.channel).localeCompare(channelLabel(right.channel), "zh-CN")
      || left.platform.localeCompare(right.platform, "zh-CN")
    );
  });
});

const credentialTypeLabels = {
  config_assignment_secret: "配置赋值密钥",
  private_key_block: "私钥块",
  db_connection_url: "数据库连接串",
  password_with_context: "上下文密码",
  api_key: "API Key",
  api_token: "API Token",
  oauth_client_secret: "OAuth Client Secret",
  ssh_private_key: "SSH 私钥",
};
const credentialTypeColors = [
  "#2865e8",
  "#4f86ee",
  "#20b8cc",
  "#7d83ef",
  "#e88912",
  "#16a477",
  "#5f7fbe",
  "#3f6fae",
  "#2e9d9a",
  "#8b63c7",
  "#c56a45",
  "#4b8d69",
];
const credentialTypes = computed(() => (overview.value.credential_summary || [])
  .map((item, index) => ({
    type: item.type,
    label: credentialTypeLabels[normalizeValue(item.type).replace(/ /g, "_")]
      || String(item.type || "未知类型").replace(/_/g, " "),
    count: Number(item.count) || 0,
    color: credentialTypeColors[index % credentialTypeColors.length],
  }))
  .filter((item) => item.count > 0));
const credentialTypeTotal = computed(() => (
  Number(overview.value.credential_type_total)
  || Number(overview.value.credential_type_count)
  || credentialTypes.value.length
));
const credentialDistributionTotal = computed(() => (
  credentialTypes.value.reduce((sum, item) => sum + item.count, 0)
));
const allCredentialsTotal = computed(() => (
  Number(overview.value.all_credentials_total)
  || Number(overview.value.credentials_total)
  || credentialDistributionTotal.value
));

function polarPoint(cx, cy, radius, angle) {
  const radians = (angle * Math.PI) / 180;
  return {
    x: cx + radius * Math.cos(radians),
    y: cy + radius * Math.sin(radians),
  };
}

function donutSegmentPath(startAngle, endAngle) {
  const outerRadius = 96;
  const innerRadius = 44;
  const outerStart = polarPoint(110, 110, outerRadius, startAngle);
  const outerEnd = polarPoint(110, 110, outerRadius, endAngle);
  const innerEnd = polarPoint(110, 110, innerRadius, endAngle);
  const innerStart = polarPoint(110, 110, innerRadius, startAngle);
  const largeArc = endAngle - startAngle > 180 ? 1 : 0;
  return [
    `M ${outerStart.x.toFixed(2)} ${outerStart.y.toFixed(2)}`,
    `A ${outerRadius} ${outerRadius} 0 ${largeArc} 1 ${outerEnd.x.toFixed(2)} ${outerEnd.y.toFixed(2)}`,
    `L ${innerEnd.x.toFixed(2)} ${innerEnd.y.toFixed(2)}`,
    `A ${innerRadius} ${innerRadius} 0 ${largeArc} 0 ${innerStart.x.toFixed(2)} ${innerStart.y.toFixed(2)}`,
    "Z",
  ].join(" ");
}

function donutSegmentTransform(angle, explosion, scale = 1) {
  const radians = (angle * Math.PI) / 180;
  const dx = Math.cos(radians) * explosion;
  const dy = Math.sin(radians) * explosion;
  return [
    `translate(${dx.toFixed(1)} ${dy.toFixed(1)})`,
    "translate(110 110)",
    `scale(${scale})`,
    "translate(-110 -110)",
  ].join(" ");
}

const credentialDonutSegments = computed(() => {
  const total = credentialDistributionTotal.value;
  const isDense = credentialTypes.value.length > 24;
  let cursor = -90;
  const largest = Math.max(1, ...credentialTypes.value.map((item) => item.count));
  return credentialTypes.value.map((item) => {
    const fraction = total ? item.count / total : 0;
    const span = fraction * 360;
    // Keep the gap smaller than the segment itself so low-count types remain visible.
    const gap = isDense
      ? Math.min(0.12, span * 0.12)
      : Math.min(7, Math.max(0.45, span * 0.16), span * 0.5);
    const startAngle = cursor + gap / 2;
    const endAngle = cursor + span - gap / 2;
    const middleAngle = cursor + span / 2;
    const explosion = item.count === largest ? 8 : 6;
    const segment = {
      ...item,
      path: donutSegmentPath(startAngle, endAngle),
      transform: donutSegmentTransform(middleAngle, explosion),
      activeTransform: donutSegmentTransform(middleAngle, explosion + 7, 1.09),
    };
    cursor += span;
    return segment;
  });
});
const activeCredentialSegment = computed(() => (
  credentialDonutSegments.value.find((item) => item.type === activeCredentialType.value) || null
));

function beijingDateKey(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

function trendDateLabel(key) {
  return key ? key.slice(5).replace("-", "-") : "—";
}

function smoothPath(points) {
  if (!points.length) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
  return points.reduce((path, point, index) => {
    if (index === 0) return `M ${point.x} ${point.y}`;
    const previous = points[index - 1];
    const middleX = ((previous.x + point.x) / 2).toFixed(1);
    return `${path} C ${middleX} ${previous.y}, ${middleX} ${point.y}, ${point.x} ${point.y}`;
  }, "");
}

const trendRows = computed(() => {
  const rows = trendFindings.value.length ? trendFindings.value : (overview.value.recent_events || []);
  const datedRows = rows.map((row) => ({
    ...row,
    dateKey: beijingDateKey(row.first_observed_at || row.first_seen_at),
    confirmed: row.review_status === "confirmed"
      || /已确认|公开泄露/.test(String(row.disposition || "")),
  })).filter((row) => row.dateKey);
  if (!datedRows.length) return [];
  const grouped = new Map();
  datedRows.forEach((row) => {
    if (!grouped.has(row.dateKey)) grouped.set(row.dateKey, []);
    grouped.get(row.dateKey).push(row);
  });
  const recordedKeys = [...grouped.keys()].sort().slice(-7);
  return recordedKeys.map((key) => {
    const dayRows = grouped.get(key) || [];
    return {
      date: key,
      label: trendDateLabel(key),
      newCount: dayRows.length,
      confirmedCount: dayRows.filter((row) => row.confirmed).length,
    };
  });
});

const trendMax = computed(() => Math.max(
  1,
  ...trendRows.value.flatMap((item) => [item.newCount, item.confirmedCount]),
));
const trendTicks = computed(() => {
  const max = trendMax.value;
  return [max, Math.round(max * 2 / 3), Math.round(max / 3), 0]
    .filter((value, index, values) => index === 0 || value !== values[index - 1]);
});
const trendGridLines = computed(() => trendTicks.value.map((value) => ({
  value,
  y: 44 + ((trendMax.value - value) / trendMax.value) * 200,
})));
const trendPoints = computed(() => {
  const rows = trendRows.value;
  const max = trendMax.value;
  const xStep = rows.length > 1 ? 674 / (rows.length - 1) : 0;
  return rows.map((item, index) => {
    const newY = 44 + ((max - item.newCount) / max) * 200;
    const confirmedY = 44 + ((max - item.confirmedCount) / max) * 200;
    const x = 66 + xStep * index;
    const tooltipX = Math.min(596, Math.max(8, x - 78));
    const tooltipY = Math.max(8, Math.min(158, Math.min(newY, confirmedY) - 84));
    return {
      ...item,
      index,
      x: Number(x.toFixed(1)),
      newY: Number(newY.toFixed(1)),
      confirmedY: Number(confirmedY.toFixed(1)),
      tooltipX: Number(tooltipX.toFixed(1)),
      tooltipY: Number(tooltipY.toFixed(1)),
    };
  });
});

function handleTrendMove(event) {
  if (!trendPoints.value.length) return;
  const svg = event.currentTarget.ownerSVGElement || event.currentTarget;
  const rect = svg.getBoundingClientRect();
  const viewX = ((event.clientX - rect.left) / rect.width) * 760;
  activeTrendIndex.value = trendPoints.value.reduce((closest, point, index) => (
    Math.abs(point.x - viewX) < Math.abs(trendPoints.value[closest].x - viewX) ? index : closest
  ), 0);
}

const trendLinePath = computed(() => ({
  new: smoothPath(trendPoints.value.map((point) => ({ x: point.x, y: point.newY }))),
  confirmed: smoothPath(trendPoints.value.map((point) => ({ x: point.x, y: point.confirmedY }))),
}));
const trendAreaPath = computed(() => ({
  new: trendPoints.value.length
    ? `${trendLinePath.value.new} L ${trendPoints.value.at(-1).x} 244 L ${trendPoints.value[0].x} 244 Z`
    : "",
  confirmed: trendPoints.value.length
    ? `${trendLinePath.value.confirmed} L ${trendPoints.value.at(-1).x} 244 L ${trendPoints.value[0].x} 244 Z`
    : "",
}));
const activeTrendPoint = computed(() => (
  activeTrendIndex.value === null ? null : trendPoints.value[activeTrendIndex.value] || null
));

function metricValue(field, label) {
  const direct = overview.value[field];
  if (direct !== undefined && direct !== null) return direct;
  return metrics.value.find((item) => String(item.label || "").includes(label))?.value ?? "—";
}

function metricStat(field, label, suffix) {
  const value = metricValue(field, label);
  return value === "—" ? "暂未统计" : `${value}${suffix}`;
}

const pipeline = computed(() => [
  {
    index: "01",
    icon: Connection,
    color: "#2463eb",
    title: "多渠道采集",
    description: "授权接口、本地载体与公开来源",
    stat: `${channelTypeCount.value} 种渠道 / 共 ${channelPlatformCount.value} 个平台`,
  },
  {
    index: "02",
    icon: Search,
    color: "#0ea5b7",
    title: "规则解析",
    description: "候选提取、过滤与证据定位",
    stat: ruleCredentialCount.value === "—"
      ? "暂未统计"
      : `${ruleCredentialCount.value} 条检测规则 / 实际检测 ${credentialTypeTotal.value} 种凭据类型`,
  },
  {
    index: "03",
    icon: DataAnalysis,
    color: "#7657c8",
    title: "指纹归一",
    description: "跨来源收敛为统一凭据实体",
    stat: metricStat("real_credentials_total", "凭据", " 个真实实体"),
  },
  {
    index: "04",
    icon: Bell,
    color: "#16845f",
    title: "告警处置",
      description: "人工确认与验证结果闭环",
    stat: pendingVerificationCount.value === "—"
      ? "暂未统计"
      : `${pendingVerificationCount.value} 条待验证凭据`,
  },
]);

async function load({ silent = false } = {}) {
  if (overviewLoadInFlight) return;
  overviewLoadInFlight = true;
  if (!silent) loading.value = true;
  if (!silent) error.value = "";
  try {
    const [stats, channels, findings, events, rules] = await Promise.all([
      systemApi.getOverview(),
      systemApi.getChannels().catch(() => ({ collectors: [] })),
      findingsApi.getFindings({
        page: 1,
        page_size: 100,
        source_kind: "全部来源",
      }).catch(() => ({ summary: {}, items: [] })),
      eventsApi.getEvents({
        page: 1,
        page_size: 100,
        source_kind: "全部来源",
        sort_by: "first_observed_at",
        sort_order: "desc",
      }).catch(() => ({ items: [], pagination: {} })),
      ruleApi.getRules(),
    ]);
    channelConnectors.value = channels.collectors || [];
    supportMatrix.value = channels.support_matrix || [];
    coverage.value = channels.coverage || stats.coverage || {};
    findingSummary.value = findings.summary || null;
    const trendItems = [...(findings.items || [])];
    const totalPages = Number(findings.pagination?.total_pages || 1);
    if (totalPages > 1) {
      const remainingPages = await Promise.all(
        Array.from({ length: totalPages - 1 }, (_, index) => (
          findingsApi.getFindings({
            page: index + 2,
            page_size: 100,
            source_kind: "全部来源",
          })
        )),
      );
      remainingPages.forEach((page) => trendItems.push(...(page.items || [])));
    }
    trendFindings.value = trendItems;
    const trendEvents = [...(events.items || [])];
    const eventTotalPages = Number(events.pagination?.total_pages || 1);
    if (eventTotalPages > 1) {
      const remainingEventPages = await Promise.all(
        Array.from({ length: eventTotalPages - 1 }, (_, index) => (
          eventsApi.getEvents({
            page: index + 2,
            page_size: 100,
            source_kind: "全部来源",
            sort_by: "first_observed_at",
            sort_order: "desc",
          })
        )),
      );
      remainingEventPages.forEach((page) => trendEvents.push(...(page.items || [])));
    }
    // 趋势按真实出现事件统计；统一发现项仅作为事件接口不可用时的回退。
    if (trendEvents.length) trendFindings.value = trendEvents;
    ruleCount.value = Array.isArray(rules) ? rules.length : Number(rules?.total || 0) || null;
    const platformSummary = (stats.by_platform || []).map(([platform, count]) => ({ platform, count }));
    overview.value = {
      ...stats,
      channel_summary: stats.channel_summary?.length
        ? stats.channel_summary
        : (stats.by_channel || []).map(([channel, objects]) => ({ channel, objects })),
      platform_summary: stats.platform_summary?.length
        ? stats.platform_summary
        : platformSummary,
      coverage: stats.coverage || channels.coverage || {},
      credential_type_total: stats.credential_type_total ?? stats.credential_type_count,
      credential_summary: stats.credential_summary?.length
        ? stats.credential_summary
        : (stats.by_type_top || []).map(([type, count]) => ({ type, count })),
    };
  } catch (e) {
    if (!silent || !Object.keys(overview.value).length) {
      error.value = e.response?.data?.detail || "总览读取失败";
    }
  } finally {
    if (!silent) loading.value = false;
    overviewLoadInFlight = false;
  }
}

async function collect() {
  running.value = true;
  try {
    await systemApi.runDue();
    ElMessage.success("已检查到期任务");
    await load();
  } finally {
    running.value = false;
  }
}

async function exportData() {
  downloadJson("evaluation-report.json", await systemApi.exportReport());
}

let overviewRefreshTimer = null;

function refreshOverviewWhenVisible() {
  if (document.visibilityState === "visible") load({ silent: true });
}

onMounted(() => {
  load();
  overviewRefreshTimer = window.setInterval(refreshOverviewWhenVisible, 15000);
  document.addEventListener("visibilitychange", refreshOverviewWhenVisible);
});

onBeforeUnmount(() => {
  if (overviewRefreshTimer) window.clearInterval(overviewRefreshTimer);
  document.removeEventListener("visibilitychange", refreshOverviewWhenVisible);
});
</script>

<style scoped>
.overview-page {
  position: relative;
  isolation: isolate;
  --overview-ink: #172338;
  --overview-muted: #6f7e92;
  --overview-line: #dbe3ec;
  --overview-surface: #fff;
  --overview-navy: #0b1628;
  --overview-cyan: #22bfd0;
  color: var(--overview-ink);
}

.overview-page::before {
  position: absolute;
  z-index: -1;
  inset: 0;
  min-height: 100%;
  background-image: url("/static/vue/branding/overview-tech-background.svg");
  background-position: top center;
  background-size: cover;
  content: "";
  opacity: .065;
  pointer-events: none;
}

.overview-hero {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 32px;
  min-height: 270px;
  margin: 0 0 18px;
  padding: 34px 40px;
  overflow: hidden;
  border: 1px solid #203a5a;
  border-radius: 14px;
  background: var(--overview-navy);
  color: #edf6ff;
}

.overview-hero::before {
  position: absolute;
  z-index: 0;
  inset: 0;
  background-image:
    linear-gradient(90deg, rgba(7, 19, 34, .72) 0%, rgba(7, 19, 34, .28) 62%, rgba(7, 19, 34, .08) 100%),
    url("/static/vue/branding/overview-tech-background.svg");
  background-position: center, center;
  background-size: cover, cover;
  content: "";
  opacity: .9;
  pointer-events: none;
}

.overview-hero::after {
  z-index: 0;
  position: absolute;
  right: 27%;
  bottom: -120px;
  width: 360px;
  height: 180px;
  border: 1px solid rgba(34, 191, 208, .2);
  border-radius: 50%;
  content: "";
  transform: rotate(-12deg);
}

.hero-copy {
  position: relative;
  z-index: 1;
  flex: 1;
  min-width: 0;
  max-width: 760px;
}

.hero-visual {
  position: relative;
  z-index: 1;
  display: grid;
  flex: 0 0 176px;
  place-items: center;
  width: 176px;
  height: 176px;
}

.hero-radar-logo {
  filter: drop-shadow(0 18px 28px rgba(0, 0, 0, .26));
}

.hero-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.hero-kicker,
.panel-kicker {
  color: #82a0c5;
  font-size: 10px;
  letter-spacing: .14em;
  text-transform: uppercase;
}

.hero-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: #a8bedb;
  font-size: 11px;
}

.hero-status i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #35c79a;
  box-shadow: 0 0 0 3px rgba(53, 199, 154, .14);
}

.overview-hero h2 {
  max-width: 680px;
  margin: 0;
  color: #f2f8ff;
  font-size: 32px;
  font-weight: 780;
  letter-spacing: 0;
  line-height: 1.35;
  white-space: normal;
  overflow-wrap: anywhere;
}

.overview-hero p {
  max-width: 700px;
  margin: 15px 0 0;
  color: #a7bad3;
  font-size: 13px;
  line-height: 1.75;
}

.hero-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 22px;
}

.overview-hero :deep(.el-button) {
  min-height: 36px;
}

.overview-hero :deep(.el-button:not(.el-button--primary):not(.is-text)) {
  border-color: #315274;
  background: rgba(255, 255, 255, .05);
  color: #d4e2f3;
}

.overview-hero :deep(.el-button:not(.el-button--primary):not(.is-text):hover) {
  border-color: #5f9bc4;
  background: rgba(95, 155, 196, .14);
  color: #fff;
}

.overview-hero :deep(.el-button.is-text) {
  color: #8ce3eb;
}

.pipeline-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 20px;
}

.pipeline-stage {
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--overview-line);
  border-radius: 10px;
  background: var(--overview-surface);
  box-shadow: 0 7px 19px rgba(29, 48, 75, .04);
  transition: border-color .18s ease, transform .18s ease;
}

.pipeline-stage:hover {
  border-color: #a4c7d5;
  transform: translateY(-2px);
}

.stage-topline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.stage-index {
  color: #b2bdca;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  letter-spacing: .12em;
}

.stage-icon {
  font-size: 19px;
}

.pipeline-stage h3 {
  margin: 13px 0 5px;
  color: #26364d;
  font-size: 13px;
  font-weight: 720;
}

.pipeline-stage p {
  min-height: 36px;
  margin: 0;
  color: var(--overview-muted);
  font-size: 11px;
  line-height: 1.55;
}

.pipeline-stage strong {
  display: block;
  margin-top: 11px;
  color: #197d8e;
  font-size: 12px;
  font-weight: 700;
}

.overview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}

.detail-grid {
  align-items: stretch;
}

.analysis-stack {
  display: grid;
  gap: 18px;
  margin-top: 18px;
}

.overview-panel {
  min-width: 0;
}

.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.panel-heading h3 {
  margin: 4px 0 0;
  color: #25344a;
  font-size: 15px;
  font-weight: 730;
}

.panel-heading :deep(.el-button) {
  flex: 0 0 auto;
}

.panel-summary,
.panel-note {
  color: var(--overview-muted);
  font-size: 12px;
}

.coverage-list {
  display: grid;
  gap: 15px;
}

.coverage-row {
  display: grid;
  gap: 7px;
}

.coverage-label {
  display: flex;
  align-items: baseline;
  gap: 7px;
  min-width: 0;
  color: #3d4b60;
  font-size: 13px;
}

.coverage-label > span:nth-child(2) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.coverage-label small {
  margin-left: auto;
  color: #8290a2;
  font-size: 11px;
  white-space: nowrap;
}

.coverage-dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.coverage-track,
.type-track {
  height: 6px;
  overflow: hidden;
  border-radius: 3px;
  background: #edf1f5;
}

.coverage-track span,
.type-track span {
  display: block;
  height: 100%;
  border-radius: inherit;
  transition: width .35s ease;
}

.platform-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 20px;
}

.panel-note {
  margin: 16px 0 0;
  line-height: 1.5;
}

.type-distribution {
  display: grid;
  grid-template-columns: 1fr;
  align-items: center;
  gap: 4px;
  min-height: 0;
}

.type-panel {
  align-self: stretch;
}

.type-donut-wrap {
  position: relative;
  display: grid;
  place-items: center;
  width: 264px;
  height: 264px;
  margin: 0 auto;
  transition: filter .24s ease;
}

.type-donut {
  width: 100%;
  height: 100%;
  overflow: visible;
}

.type-donut-segment {
  stroke: #fff;
  stroke-width: 1.8;
  stroke-linejoin: round;
  cursor: pointer;
  transition: filter .18s ease, opacity .18s ease;
}

.type-donut-segment:not(.is-active) {
  opacity: .98;
}

.type-donut-segment.is-active {
  opacity: 1;
  filter: drop-shadow(0 8px 8px rgba(40, 101, 232, .2));
}

.type-donut.is-dense .type-donut-segment {
  stroke-width: .75;
}

.type-donut-tooltip {
  position: absolute;
  top: 50%;
  left: 50%;
  display: grid;
  min-width: 108px;
  padding: 8px 13px 9px;
  border: 1px solid rgba(40, 101, 232, .14);
  border-radius: 12px;
  background: rgba(255, 255, 255, .96);
  box-shadow: 0 9px 24px rgba(45, 75, 120, .16);
  color: #46627e;
  pointer-events: none;
  text-align: center;
  transform: translate(-50%, -50%);
}

.type-donut-tooltip span {
  overflow: hidden;
  color: #71839a;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.type-donut-tooltip strong {
  margin-top: 1px;
  color: #245ed9;
  font-size: 23px;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}

.type-donut-tooltip small {
  color: #8a99ab;
  font-size: 10px;
}

.type-legend {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 2px 12px;
  min-width: 0;
  max-height: 320px;
  padding: 0 10px 2px;
  overflow-y: auto;
  scrollbar-color: #b9cbe2 transparent;
  scrollbar-width: thin;
}

.type-legend-row {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  width: auto;
  min-height: 24px;
  padding: 2px 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: #596b82;
  cursor: pointer;
  font: inherit;
  text-align: left;
  transition: border-color .18s ease, background .18s ease, color .18s ease;
}

.type-legend-row:hover,
.type-legend-row:focus-visible,
.type-legend-row.is-active {
  color: #254c82;
  outline: none;
}

.type-legend-swatch {
  display: block;
  flex: 0 0 8px;
  width: 8px;
  height: 8px;
  min-width: 8px;
  border-radius: 2px;
}

.type-legend-label {
  min-width: 0;
  overflow: hidden;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.type-legend-row strong {
  color: #2b4568;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.verification-chart {
  display: grid;
  gap: 22px;
  padding-top: 8px;
}

.verification-track {
  display: flex;
  width: 100%;
  height: 14px;
  overflow: hidden;
  border-radius: 7px;
  background: #edf1f5;
}

.verification-track span {
  display: block;
  min-width: 4px;
  height: 100%;
  transition: width .35s ease;
}

.verification-legend {
  display: grid;
  gap: 12px;
}

.trend-panel {
  min-height: 382px;
}

.trend-heading {
  align-items: flex-start;
}

.trend-heading p {
  margin: 6px 0 0;
  color: #8493a7;
  font-size: 11px;
}

.trend-legend {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 14px;
  padding-top: 6px;
  color: #71829a;
  font-size: 11px;
}

.trend-legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.trend-scope {
  color: #4e6f9d;
  font-weight: 650;
}

.trend-dot {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

.trend-dot--new {
  background: #2865e8;
}

.trend-dot--confirmed {
  background: #20c7dc;
}

.trend-chart-wrap {
  position: relative;
  display: grid;
  min-height: 278px;
  padding: 4px 0 0;
}

.trend-y-axis {
  position: absolute;
  top: 38px;
  bottom: 44px;
  left: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  width: 34px;
  color: #8190a4;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  pointer-events: none;
  text-align: right;
}

.trend-chart {
  width: 100%;
  min-height: 280px;
  overflow: visible;
}

.trend-grid-line {
  stroke: #e5edf7;
  stroke-width: 1;
}

.trend-area {
  pointer-events: none;
}

.trend-area--new {
  fill: url("#newTrendFill");
}

.trend-area--confirmed {
  fill: url("#confirmedTrendFill");
}

.trend-line {
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 3;
  pointer-events: none;
}

.trend-line--new {
  stroke: #2865e8;
}

.trend-line--confirmed {
  stroke: #20c7dc;
}

.trend-hover-line {
  stroke: #8ea1bb;
  stroke-width: 1;
  stroke-dasharray: 4 5;
  pointer-events: none;
}

.trend-hover-zone {
  fill: #ffffff;
  fill-opacity: .001;
  cursor: crosshair;
  pointer-events: all;
}

.trend-point {
  stroke: #fff;
  stroke-width: 2;
  pointer-events: none;
}

.trend-point--new {
  fill: #2865e8;
}

.trend-point--confirmed {
  fill: #20c7dc;
}

.trend-point-hit {
  fill: transparent;
  cursor: crosshair;
  outline: none;
}

.trend-point-hit:focus-visible + .trend-point {
  stroke: #1d3762;
  stroke-width: 3;
}

.trend-x-label {
  fill: #71829a;
  font-size: 11px;
}

.trend-tooltip rect {
  fill: #fff;
  stroke: #d6e1ee;
  stroke-width: 1;
  filter: drop-shadow(0 7px 14px rgba(29, 48, 75, .1));
}

.trend-tooltip-title,
.trend-tooltip text {
  fill: #566b86;
  font-size: 11px;
}

.trend-tooltip-title {
  fill: #263b5b;
  font-weight: 700;
}

.trend-tooltip-value {
  fill: #263b5b;
  font-size: 11px;
  font-weight: 750;
  text-anchor: end;
}

@media (max-width: 1100px) {
  .pipeline-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .pipeline-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .overview-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .overview-hero {
    flex-direction: column;
    align-items: flex-start;
    gap: 18px;
    min-height: 0;
    padding: 26px 22px;
  }

  .overview-hero h2 {
    font-size: 24px;
  }

  .hero-copy {
    width: 100%;
    max-width: 100%;
  }

  .hero-visual {
    order: -1;
    flex: 0 0 56px;
    width: 56px;
    height: 56px;
  }

  .hero-radar-logo {
    transform: none;
  }

  .hero-actions {
    position: relative;
    z-index: 2;
  }

  .hero-actions :deep(.el-button) {
    flex: 1 1 auto;
    margin-left: 0;
  }

  .pipeline-grid,
  .overview-grid {
    grid-template-columns: 1fr;
  }

  .pipeline-stage p {
    min-height: 0;
  }

  .coverage-label {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .coverage-label small {
    width: 100%;
    margin-left: 14px;
  }

  .type-legend {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 480px) {
  .overview-hero {
    padding: 22px 18px;
  }

  .overview-hero h2 {
    max-width: 100%;
    font-size: 24px;
  }

  .overview-hero p {
    max-width: 100%;
    font-size: 12px;
  }

  .hero-copy {
    max-width: 100%;
  }

  .hero-visual {
    position: static;
  }

  .hero-radar-logo {
    transform: none;
  }

  .hero-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .hero-actions :deep(.el-button) {
    justify-content: center;
    width: 100%;
  }

  .type-distribution {
    grid-template-columns: 1fr;
  }

  .type-donut-wrap {
    width: 246px;
    height: 246px;
  }

  .trend-panel {
    min-height: 0;
  }

  .trend-heading {
    display: grid;
    gap: 10px;
  }

  .trend-legend {
    justify-content: flex-start;
  }

}

@media (prefers-reduced-motion: reduce) {
  .pipeline-stage,
  .coverage-track span,
  .type-track span {
    transition: none;
  }
}
</style>
