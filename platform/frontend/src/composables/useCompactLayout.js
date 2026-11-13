import { onMounted, onUnmounted, ref } from "vue";

export function useCompactLayout(query = "(max-width: 900px)") {
  const media = window.matchMedia(query);
  const compact = ref(media.matches);
  const update = event => { compact.value = event.matches; };
  onMounted(() => media.addEventListener("change", update));
  onUnmounted(() => media.removeEventListener("change", update));
  return compact;
}
