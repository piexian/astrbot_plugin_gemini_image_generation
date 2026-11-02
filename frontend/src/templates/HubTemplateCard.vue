<script setup lang="ts">
import { computed } from "vue";
import type { HubTemplate, Translator } from "./types";

const props = defineProps<{
  template: HubTemplate;
  mine: boolean;
  t: Translator;
}>();

const emit = defineEmits<{
  view: [];
  edit: [];
  drop: [];
  report: [];
  use: [];
  import: [];
}>();

const tags = computed(() => props.template.tags || []);
const defaultsText = computed(() => {
  const parts: string[] = [];
  if (props.template.defaults?.resolution) parts.push(props.template.defaults.resolution);
  if (props.template.defaults?.aspect_ratio) parts.push(props.template.defaults.aspect_ratio);
  return parts.join(" · ");
});
function referenceLabel(value?: string) {
  const t = props.t;
  if (value === "required") return t("需要参考图");
  if (value === "optional") return t("可选参考图");
  return t("不需要参考图");
}
const sourcePill = computed(() => {
  if (props.mine) return props.t("我的");
  return props.template.found_via === "online" ? props.t("实时检索") : props.t("缓存");
});
</script>

<template>
  <article class="tpl-card comic-card">
    <div class="tpl-card-head">
      <h3 class="tpl-card-title">{{ template.name }}</h3>
      <span class="meta-pill">{{ sourcePill }}</span>
    </div>
    <p class="tpl-card-desc">{{ template.ai_description || "" }}</p>
    <div class="tpl-card-tags">
      <span v-for="tag in tags" :key="tag" class="meta-pill">{{ tag }}</span>
    </div>
    <p class="tpl-card-meta">
      {{ referenceLabel(template.reference)
      }}<template v-if="defaultsText"> · {{ defaultsText }}</template>
    </p>
    <p v-if="template.author" class="tpl-card-meta">{{ template.author }}</p>
    <div class="tpl-card-actions">
      <button
        type="button"
        class="comic-btn comic-btn--sm comic-btn--outline"
        @click="emit('view')"
      >
        {{ t("查看") }}
      </button>
      <template v-if="mine">
        <button
          type="button"
          class="comic-btn comic-btn--sm comic-btn--outline"
          @click="emit('edit')"
        >
          {{ t("修改") }}
        </button>
        <button
          type="button"
          class="comic-btn comic-btn--sm comic-btn--danger"
          @click="emit('drop')"
        >
          {{ t("删除") }}
        </button>
      </template>
      <button
        v-else
        type="button"
        class="comic-btn comic-btn--sm comic-btn--outline"
        @click="emit('report')"
      >
        {{ t("举报") }}
      </button>
      <button type="button" class="comic-btn comic-btn--sm comic-btn--cta" @click="emit('use')">
        {{ t("填入工作台") }}
      </button>
      <button
        v-if="!mine"
        type="button"
        class="comic-btn comic-btn--sm comic-btn--outline"
        @click="emit('import')"
      >
        {{ t("导入为本地") }}
      </button>
    </div>
  </article>
</template>
