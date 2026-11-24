import {
  ClipboardCopy,
  FileSpreadsheet,
  Gauge,
  GitCompareArrows,
  Hexagon,
  ImagePlus,
  Info,
  Keyboard,
  Library,
  ListChecks,
  Pencil,
  ScanText,
  Share2,
  Type,
  type LucideIcon
} from "lucide-react";
import type { ReactNode } from "react";
import { PREDICT_PROPERTY_CATALOG, PREDICT_PROPERTY_GROUPS } from "../../constants/predictableProperties";
import { ChannelBadge } from "./channelIcons";
import { scaleRangeLabel } from "./predictionScale";
import { STRUCTURE_FACTS } from "./structureFacts";

const SECTION_IDS = {
  input: "np-hp-help-input",
  properties: "np-hp-help-properties",
  reading: "np-hp-help-reading",
  compare: "np-hp-help-compare",
  export: "np-hp-help-export",
  keys: "np-hp-help-keys"
} as const;

const INPUT_WAYS: readonly { Icon: LucideIcon; title: string; text: ReactNode }[] = [
  { Icon: Pencil, title: "画板绘制", text: <>直接在画板上画，用 <code>*</code> 原子标出连接点</> },
  { Icon: Type, title: "粘贴 SMILES", text: "在画板下方的 SMILES 框输入，画板随之更新" },
  { Icon: ImagePlus, title: "导入图片", text: "识别结构图片，载入后可以继续修改" },
  { Icon: Library, title: "示例与我的结构", text: "从“示例”或“我的结构”载入已有结构" }
];

function HelpSection({
  id,
  icon: Icon,
  title,
  children,
  className
}: {
  id: string;
  icon: LucideIcon;
  title: string;
  children: ReactNode;
  className?: string;
}) {
  const headingId = `${id}-title`;
  return (
    <section id={id} className={["np-hp-help__section", className].filter(Boolean).join(" ")} aria-labelledby={headingId}>
      <h3 id={headingId}>
        <span className="np-hp-help__glyph" aria-hidden="true"><Icon /></span>
        {title}
      </h3>
      {children}
    </section>
  );
}

/** In-page help shown in the module dock: quick start, an index, then one card per topic. */
export function HomopolymerPredictionHelp({ structureFactsId = "np-hp-help-structure-facts" }: { structureFactsId?: string }) {
  const index = [
    { id: SECTION_IDS.input, label: "输入结构" },
    { id: structureFactsId, label: "结构信息" },
    { id: SECTION_IDS.properties, label: "预测内容" },
    { id: SECTION_IDS.reading, label: "读懂结果" },
    { id: SECTION_IDS.compare, label: "运行与对比" },
    { id: SECTION_IDS.export, label: "导出" },
    { id: SECTION_IDS.keys, label: "快捷键" }
  ];

  return (
    <div className="np-hp-help">
      <p className="np-hp-help__lead">输入一个重复单元结构，选择要预测的性质，运行后在右侧查看结果，并和本次会话里更早的运行对比。</p>

      <ol className="np-hp-help__steps" aria-label="快速上手">
        <li>
          <span className="np-hp-help__step" aria-hidden="true">1</span>
          <b>画出结构</b>
          <small>画板、SMILES 或图片</small>
        </li>
        <li>
          <span className="np-hp-help__step" aria-hidden="true">2</span>
          <b>选择性质</b>
          <small>默认全部 9 项</small>
        </li>
        <li>
          <span className="np-hp-help__step" aria-hidden="true">3</span>
          <b>运行并对比</b>
          <small>Ctrl / ⌘ + Enter</small>
        </li>
      </ol>

      <nav className="np-hp-help__index" aria-label="说明目录">
        {index.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => document.getElementById(item.id)?.scrollIntoView({ block: "start", behavior: "smooth" })}
          >
            {item.label}
          </button>
        ))}
      </nav>

      <HelpSection id={SECTION_IDS.input} icon={Hexagon} title="输入结构">
        <ul className="np-hp-help__ways">
          {INPUT_WAYS.map(({ Icon, title, text }) => (
            <li key={title}>
              <Icon aria-hidden="true" />
              <b>{title}</b>
              <span>{text}</span>
            </li>
          ))}
        </ul>
        <p className="np-hp-help__tip">
          普通 SMILES 和带 <code>*</code> 连接点的重复单元都可以预测。重复单元建议用两个 <code>*</code> 标出连接点，例如聚苯乙烯 <code>*CC(*)c1ccccc1</code>。
        </p>
      </HelpSection>

      <HelpSection id={structureFactsId} icon={ScanText} title="结构信息" className="np-hp-help__facts">
        <p>画板下方的一行数值由 RDKit 根据当前结构直接计算，用来检查结构、帮助解读结果，不是模型预测值。连接点 <code>*</code> 不计入原子数和分子式。</p>
        <dl className="np-hp-help__terms">
          {STRUCTURE_FACTS.map((fact) => (
            <div key={fact.key}>
              <dt>{fact.label}{fact.unit ? <span>{fact.unit}</span> : null}</dt>
              <dd>{fact.explanation}</dd>
            </div>
          ))}
        </dl>
        <p className="np-hp-help__aside">这些只是常见趋势，同一结构的实际性能还取决于聚合度、结晶度、加工条件等。</p>
      </HelpSection>

      <HelpSection id={SECTION_IDS.properties} icon={ListChecks} title="预测内容">
        <p>共 9 项，分三组。右列是结果刻度条使用的显示区间。</p>
        {PREDICT_PROPERTY_GROUPS.map((group) => (
          <div key={group.key} className={`np-hp-help__group is-${group.key}`}>
            <h4><ChannelBadge group={group.key} />{group.label}</h4>
            <ul>
              {PREDICT_PROPERTY_CATALOG.filter((property) => property.group === group.key).map((property) => (
                <li key={property.key}>
                  <span>{property.label}</span>
                  <small>{scaleRangeLabel(property.key, property.unit)}</small>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </HelpSection>

      <HelpSection id={SECTION_IDS.reading} icon={Gauge} title="读懂结果">
        <ul className="np-hp-help__legend">
          <li>
            <span className="np-hp-help__sample" aria-hidden="true">
              <span className="np-hp-help__rail">
                <i style={{ left: "30%", width: "34%" }} />
                <b style={{ left: "64%" }} />
                <b className="is-ring" style={{ left: "30%" }} />
              </span>
            </span>
            <span>
              <b>刻度条</b>
              实心点是本次结果，空心圈是对比的那次运行。区间固定，只为方便比较，不代表数据库里的分布；超出区间的读数停在端点并带箭头。断裂伸长率和三种渗透率用对数刻度。鼠标移到一行上显示区间两端。
            </span>
          </li>
          <li>
            <span className="np-hp-help__sample" aria-hidden="true">
              <span className="np-hp-delta is-up">+12</span>
              <span className="np-hp-delta is-down">−8</span>
            </span>
            <span>
              <b>变化</b>
              绿色表示比对比运行高，红色表示低。颜色只表示升降，不代表好坏：同样是升高，对 Tg 和对渗透率意义不同。
            </span>
          </li>
          <li>
            <span className="np-hp-help__sample" aria-hidden="true">
              <span className="np-hp-delta is-large is-up">+50</span>
            </span>
            <span>
              <b>变化达到 20%</b>
              相对对比值变化 20% 及以上时，加粗并带底色。
            </span>
          </li>
          <li>
            <span className="np-hp-help__sample is-text" aria-hidden="true">未返回</span>
            <span>
              <b>没有返回值</b>
              模型没有给出这一项，不会显示为 0。
            </span>
          </li>
        </ul>
      </HelpSection>

      <HelpSection id={SECTION_IDS.compare} icon={GitCompareArrows} title="运行与对比">
        <ul className="np-hp-help__list">
          <li>从第二次运行起，结果自动与上一次对比；在“对比”里可以改选本次会话里更早的运行，或选“不对比”。</li>
          <li>“本次会话的运行”最多保留 10 次，点一条可恢复当时的结构和结果。刷新页面后清空。</li>
          <li>修改结构或性质选择后，旧结果保留并标出“已变化”，重新运行即可更新。</li>
        </ul>
      </HelpSection>

      <HelpSection id={SECTION_IDS.export} icon={Share2} title="导出">
        <ul className="np-hp-help__ways is-two">
          <li>
            <FileSpreadsheet aria-hidden="true" />
            <b>下载 CSV</b>
            <span>模型输出的原始数值，对比时带对比列和变化列</span>
          </li>
          <li>
            <ClipboardCopy aria-hidden="true" />
            <b>复制表格</b>
            <span>可以直接粘贴到 Excel 等表格软件</span>
          </li>
        </ul>
      </HelpSection>

      <HelpSection id={SECTION_IDS.keys} icon={Keyboard} title="快捷键">
        <dl className="np-hp-help__keys">
          <div>
            <dt>运行预测</dt>
            <dd><kbd>Ctrl</kbd> + <kbd>Enter</kbd><small>Mac 上为 <kbd>⌘</kbd> + <kbd>Enter</kbd></small></dd>
          </div>
          <div>
            <dt>在菜单里移动、选择</dt>
            <dd><kbd>↑</kbd> <kbd>↓</kbd> <kbd>Enter</kbd></dd>
          </div>
          <div>
            <dt>关闭菜单或辅助栏</dt>
            <dd><kbd>Esc</kbd></dd>
          </div>
          <div>
            <dt>跳过画板工具</dt>
            <dd>页面开头按 <kbd>Tab</kbd></dd>
          </div>
        </dl>
      </HelpSection>

      <p className="np-hp-help__note">
        <Info aria-hidden="true" />
        <span>这些是模型预测值，用于研究辅助，需要结合适用范围、已有数据和实验验证判断。</span>
      </p>
    </div>
  );
}
