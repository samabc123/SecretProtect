import vue from "eslint-plugin-vue";

export default [
  ...vue.configs["flat/recommended"],
  {
    files: ["**/*.{js,vue}"],
    languageOptions: {
      globals: {
        window: "readonly",
        document: "readonly",
        Blob: "readonly",
        URL: "readonly",
        Intl: "readonly",
        console: "readonly",
      },
    },
    rules: {
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
      "vue/multi-word-component-names": "off",
      "vue/max-attributes-per-line": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/multiline-html-element-content-newline": "off",
      "vue/require-default-prop": "off",
      "vue/attributes-order": "off",
    },
  },
];
