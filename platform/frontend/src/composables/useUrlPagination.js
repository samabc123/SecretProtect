import { reactive } from "vue";
import { useRoute, useRouter } from "vue-router";

function positiveInteger(value, fallback) {
  const parsed = Number.parseInt(String(value || ""), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function useUrlPagination({
  prefix = "",
  defaultPageSize = 10,
  pageSizes = [10, 20, 50],
} = {}) {
  const route = useRoute();
  const router = useRouter();
  const pageKey = prefix ? `${prefix}Page` : "page";
  const sizeKey = prefix ? `${prefix}PageSize` : "pageSize";
  const pagination = reactive({
    page: positiveInteger(route.query[pageKey], 1),
    page_size: pageSizes.includes(Number(route.query[sizeKey]))
      ? Number(route.query[sizeKey])
      : defaultPageSize,
    total_items: 0,
    total_pages: 0,
  });

  async function syncQuery(extra = {}) {
    const query = { ...route.query, ...extra };
    query[pageKey] = String(pagination.page);
    query[sizeKey] = String(pagination.page_size);
    await router.replace({
      path: route.path,
      hash: route.hash,
      query,
    });
  }

  async function setPage(page) {
    pagination.page = Math.max(1, Number(page) || 1);
    await syncQuery();
  }

  async function setPageSize(pageSize) {
    pagination.page_size = pageSizes.includes(Number(pageSize))
      ? Number(pageSize)
      : defaultPageSize;
    pagination.page = 1;
    await syncQuery();
  }

  function applyResponse(value = {}) {
    Object.assign(pagination, {
      ...value,
      page: positiveInteger(value.page, pagination.page),
      page_size: positiveInteger(value.page_size, pagination.page_size),
      total_items: Number(value.total_items || 0),
      total_pages: Number(value.total_pages || 0),
    });
  }

  return {
    pagination,
    pageSizes,
    syncQuery,
    setPage,
    setPageSize,
    applyResponse,
  };
}

export function slicePage(items, pagination) {
  const total = items.length;
  const totalPages = total ? Math.ceil(total / pagination.page_size) : 0;
  if (totalPages && pagination.page > totalPages) pagination.page = totalPages;
  const start = (Math.max(1, pagination.page) - 1) * pagination.page_size;
  return {
    items: items.slice(start, start + pagination.page_size),
    pagination: {
      page: totalPages ? Math.min(pagination.page, totalPages) : 1,
      page_size: pagination.page_size,
      total_items: total,
      total_pages: totalPages,
    },
  };
}
