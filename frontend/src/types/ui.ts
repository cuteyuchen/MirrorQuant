// 页面表格只描述展示列，不把接口类型变成任意字典。
export interface TableColumn {
  key: string;
  label: string;
  numeric?: boolean;
  width?: string;
  mobileWide?: boolean;
}
