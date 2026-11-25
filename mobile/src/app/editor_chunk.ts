// 编辑器分包的入口。主包只 import() 这一个文件：编辑器、导出（选项、就绪条、共享）、⋯ 菜单、复制 / 粘贴面板
// 和关机图都由这里静态带进来。打开编辑器以后，里面的面板彼此都是静态导入，不再有懒加载。
// 显卡引擎在主包里（boot_lazy.ts），开始页空闲时就建，点开第一张照片不用等它。

export { EditorView } from './editor';
export { ExportFlow, syncExportDock } from './export_ui';
