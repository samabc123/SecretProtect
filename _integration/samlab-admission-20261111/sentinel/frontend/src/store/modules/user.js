import { defineStore } from "pinia";
import * as authApi from "../../api/auth";

export const useUserStore = defineStore("user", {
  state: () => ({ user: null, loaded: false }),
  getters: { isLoggedIn: (state) => Boolean(state.user) },
  actions: {
    async load() {
      try {
        const payload = await authApi.me();
        this.user = payload.user;
      } finally {
        this.loaded = true;
      }
    },
    async login(payload) {
      const result = await authApi.login(payload);
      document.querySelector('meta[name="csrf-token"]').content = result.csrf_token;
      this.user = result.user;
      return result;
    },
    async logout() {
      await authApi.logout();
      this.user = null;
    },
  },
});
