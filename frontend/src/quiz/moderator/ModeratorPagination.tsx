export function ModeratorPagination({ page, pageCount, count, label, onPageChange }: {
  page: number;
  pageCount: number;
  count: number;
  label: string;
  onPageChange: (page: number) => void;
}) {
  if (pageCount <= 1) return null;

  return (
    <nav className="moderator-pagination" aria-label={label}>
      <button type="button" disabled={page === 0} onClick={() => onPageChange(page - 1)}>前へ</button>
      <span aria-live="polite">{page + 1} / {pageCount} ページ · 全{count}人</span>
      <button type="button" disabled={page === pageCount - 1} onClick={() => onPageChange(page + 1)}>次へ</button>
    </nav>
  );
}
