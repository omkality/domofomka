import react from "@astrojs/react";
import { unified } from "@astrojs/markdown-remark";
import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";
import { LikeC4VitePlugin } from "likec4/vite-plugin";
import { documentationLinks } from "./scripts/documentation-links.mjs";

const base = process.env.DOCS_BASE_PATH || "/";

export default defineConfig({
  site: process.env.DOCS_SITE_URL || "http://localhost:4321",
  base,
  output: "static",
  server: { host: "127.0.0.1", port: 4321 },
  markdown: { processor: unified({ remarkPlugins: [[documentationLinks, { base }]] }) },
  vite: { plugins: [LikeC4VitePlugin({ workspace: "architecture" })] },
  integrations: [
    react(),
    starlight({
      title: "domofomka",
      description: "Архитектура, поиск адресов, надёжная доставка и эксплуатация бота.",
      defaultLocale: "root",
      locales: { root: { label: "Русский", lang: "ru" } },
      social: [{ icon: "github", label: "GitHub", href: "https://github.com/omkality/domofomka/tree/dev" }],
      customCss: ["./src/styles/custom.css"],
      sidebar: [
        { label: "Проект", items: [
          { label: "Обзор", slug: "index" },
          { label: "Архитектура C4", slug: "architecture" },
          { label: "Процессы по шагам", slug: "processes" },
          { label: "Схема БД", slug: "database" },
        ] },
        { label: "Работа бота", items: [
          { label: "Поиск v2", slug: "search-v2" },
          { label: "Адресный язык", slug: "address-language" },
          { label: "Inbox / outbox", slug: "message-delivery" },
          { label: "Ограничения очереди", slug: "message-rate-limit" },
          { label: "Измерения запросов", slug: "telemetry" },
        ] },
        { label: "Эксплуатация", items: [
          { label: "Запуск и ресурсы", slug: "operations" },
          { label: "CI/CD и откат", slug: "cicd" },
          { label: "Хранение и диск", slug: "storage-safety" },
          { label: "Стенд dev", slug: "dev-stand" },
          { label: "Разработка и проверки", slug: "development" },
          { label: "Индексы PostgreSQL", slug: "catalog-street-layout" },
        ] },
        { label: "Исследования и история", items: [{ autogenerate: { directory: "research" } }] },
      ],
    }),
  ],
});
