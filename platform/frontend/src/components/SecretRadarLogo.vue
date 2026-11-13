<template>
  <span class="sr-logo" :class="{ 'sr-hover': hoverOnly }" :style="{ width: `${size}px`, height: `${size}px` }">
    <svg :width="size" :height="size" viewBox="0 0 100 100" role="img" aria-label="SecretRadar">
      <defs>
        <linearGradient :id="gidBg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" :stop-color="variant === 'dark' ? '#0a1122' : '#3b82f6'" />
          <stop offset="1" :stop-color="variant === 'dark' ? '#122a4d' : '#22d3ee'" />
        </linearGradient>
        <linearGradient :id="gidSweep" x1="1" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#22d3ee" stop-opacity=".5" />
          <stop offset="1" stop-color="#22d3ee" stop-opacity="0" />
        </linearGradient>
      </defs>

      <rect width="100" height="100" rx="24" :fill="`url(#${gidBg})`" />
      <circle cx="50" cy="50" r="38" fill="none" :stroke="ringColor" stroke-width="2.2" stroke-opacity=".95" />
      <circle cx="50" cy="50" r="22" fill="none" :stroke="ringColor" stroke-width="1.4" stroke-opacity=".4" />
      <circle cx="50" cy="50" r="2.6" :fill="ringColor" />

      <g class="sr-sweep" :class="{ 'sr-spin': animated }">
        <path d="M50 50 L84 16 A40 40 0 0 1 90 50 Z" :fill="`url(#${gidSweep})`" />
        <line
          x1="50"
          y1="50"
          x2="84"
          y2="16"
          :stroke="ringColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-opacity=".9"
        />
      </g>

      <g
        class="sr-shield"
        aria-hidden="true"
        transform="translate(50 53) scale(.92) translate(-50 -54.5)"
      >
        <path
          d="M50 33 L68 38.8 V50.2 C68 61.4 60.8 70.7 50 75.5 C39.2 70.7 32 61.4 32 50.2 V38.8 Z"
          fill="#f2fbff"
        />
        <path d="M47.2 49.5 H52.8 V67 H47.2 Z" fill="#0b1b31" />
        <path d="M52.8 60.5 H56.2 V64.7 H52.8 Z" fill="#0b1b31" />
        <circle cx="50" cy="46" r="7.5" fill="#0b1b31" />
        <circle cx="50" cy="46" r="3.8" fill="#f2fbff" />
      </g>

      <circle cx="76" cy="24" r="3.4" fill="#22d3ee" />
      <circle cx="76" cy="24" r="6" fill="none" stroke="#22d3ee" stroke-width="1" stroke-opacity=".5" />
    </svg>
  </span>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  size: { type: Number, default: 32 },
  animated: { type: Boolean, default: true },
  hoverOnly: { type: Boolean, default: false },
  variant: { type: String, default: "gradient" },
});

const ringColor = computed(() => props.variant === "dark" ? "#67e8f9" : "#ffffff");
const uid = Math.random().toString(36).slice(2, 8);
const gidBg = `srBg-${uid}`;
const gidSweep = `srSweep-${uid}`;
</script>

<style scoped>
.sr-logo { display: inline-flex; line-height: 0; }

.sr-sweep {
  transform-origin: 50px 50px;
  animation: sr-spin 4s linear infinite;
  animation-play-state: paused;
}

.sr-sweep.sr-spin { animation-play-state: running; }
.sr-hover .sr-sweep { animation-play-state: paused !important; }
.sr-hover:hover .sr-sweep { animation-play-state: running !important; }
.sr-shield { pointer-events: none; }

@keyframes sr-spin { to { transform: rotate(360deg); } }

@media (prefers-reduced-motion: reduce) {
  .sr-sweep { animation: none; }
}
</style>
