# Miro - an RSS based video player application
# Copyright (C) 2005-2008 Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

"""itemlist.py -- Handles TableModel objects that store items.

itemlist, itemlistcontroller and itemlistwidgets work togetherusing the MVC
pattern.  itemlist handles the Model, itemlistwidgets handles the View and
itemlistcontroller handles the Controller.

ItemList manages a TableModel that stores ItemInfo objects.  It handles
filtering out items from the list (for example in the Downloading items list).
They also handle temporarily filtering out items based the user's search
terms.
"""

from miro import search
from miro.frontends.widgets import imagepool
from miro.plat.frontends.widgets import widgetset

def item_matches_search(item_info, search_text):
    """Check if an item matches search text."""
    if search_text == '':
        return True
    match_against = [item_info.name, item_info.description]
    if item_info.video_path is not None:
        match_against.append(item_info.video_path)
    return search.match(search_text, match_against)

class ItemSort(object):
    """Class that sorts items in an item list."""

    def __init__(self):
        self.reverse = False

    def reverse(self):
        """Reverse the order of the sort."""
        self.reverse = not self.reverse

    def sort_key(self, item):
        """Return a value that can be used to sort item.
        
        Must be implemented by sublcasses.
        """
        raise NotImplentedError()

    def compare(self, item, other):
        """Compare two items
        
        Returns -1 if item < other, 1 if other > item and 0 if item == other
        (same as cmp)
        """
        return cmp(self.sort_key(item), self.sort_key(other))

    def sort_items(self, item_list):
        """Sort a list of items (in place)."""
        item_list.sort(key=self.sort_key, reverse=self.reverse)

class DateSort(ItemSort):
    def __init__(self):
        self.reverse = True

    def sort_key(self, item):
        return item.release_date

class ItemListGroup(object):
    """Manages a set of ItemLists.

    ItemListGroup keep track of one or more ItemLists.  When items are
    added/changed/removed they take care of making sure each child list
    updates itself.  
    
    ItemLists maintain an item sorting and a search filter that are shared by
    each child list.
    """

    def __init__(self, item_lists):
        """Construct in ItemLists.  
        
        item_lists is a list of ItemList objects that should be grouped
        together.
        """
        self.item_lists = item_lists
        self.set_sort(DateSort())

    def _setup_info(self, info):
        """Initialize a newly recieved ItemInfo."""
        info.icon = imagepool.LazySurface(info.thumbnail, (154, 105))

    def add_items(self, item_list):
        """Add a list of new items to the item list.
        
        Note: This method will sort item_list
        """
        self._sorter.sort_items(item_list)
        for item_info in item_list:
            self._setup_info(item_info)
        for sublist in self.item_lists:
            sublist.add_items(item_list, already_sorted=True)

    def update_items(self, changed_items):
        """Update items.
        
        Note: This method will sort changed_items
        """
        self._sorter.sort_items(changed_items)
        for item_info in changed_items:
            self._setup_info(item_info)
        for sublist in self.item_lists:
            sublist.update_items(changed_items, already_sorted=True)

    def remove_items(self, removed_ids):
        """Remove items from the list."""
        for sublist in self.item_lists:
            sublist.remove_items(removed_ids)

    def set_sort(self, sorter):
        """Change the way items are sorted in the list (and filtered lists)

        sorter must be a subclass of ItemSort.
        """
        self._sorter = sorter
        for sublist in self.item_lists:
            sublist.set_sort(sorter)

    def set_search_text(self, search_text):
        """Update the search for each child list."""
        for sublist in self.item_lists:
            sublist.set_search_text(search_text)

class ItemList(object):
    """
    Attributes:

    model -- TableModel for this item list.  It contains 2 columns, ItemInfo
    objects and a show_details boolean flag.
    """

    def __init__(self):
        self.model = widgetset.TableModel('object', 'boolean')
        self._iter_map = {}
        self._sorter = None
        self._search_text = ''
        self._non_matching_items = {} 
        # maps ids -> items that don't match the search

    def set_sort(self, sorter):
        self._sorter = sorter
        self._resort_items()

    def get_count(self):
        """Get the number of items in this list."""
        return len(self.model)

    def get_items(self, start_id=None):
        """Get a list of ItemInfo objects in this list"""
        if start_id is None:
            return [row[0] for row in self.model]
        else:
            iter = self._iter_map[start_id]
            retval = []
            while iter is not None:
                retval.append(self.model[iter][0])
                iter = self.model.next_iter(iter)
            return retval

    def _resort_items(self):
        rows = []
        iter = self.model.first_iter()
        while iter is not None:
            rows.append(tuple(self.model[iter]))
            iter = self.model.remove(iter)
        rows.sort(key=lambda row: self._sorter.sort_key(row[0]))
        for row in rows:
            self._iter_map[row[0].id] = self.model.append(row)

    def filter(self, item_info):
        """Can be overrided by subclasses to filter out items from the list.
        """
        return True

    def _should_show_item(self, item_info):
        """Decide if an item should be shown."""
        if not self.filter(item_info):
            return False
        if not item_matches_search(item_info, self._search_text):
            self._non_matching_items[item_info.id] = item_info
            return False
        else:
            try:
                del self._non_matching_items[item_info.id]
            except KeyError:
                pass
            return True

    def set_show_details(self, item_id, value):
        """Change the show details value for an item"""
        iter = self._iter_map[item_id]
        self.model.update_value(iter, 1, value)

    def _insert_sorted_items(self, item_list):
        pos = self.model.first_iter()
        for item_info in item_list:
            while (pos is not None and 
                    self._sorter.compare(self.model[pos][0], item_info) < 0):
                pos = self.model.next_iter(pos)
            iter = self.model.insert_before(pos, item_info, False)
            self._iter_map[item_info.id] = iter

    def add_items(self, item_list, already_sorted=False):
        if not already_sorted:
            self._sorter.sort_items(item_list)
        self._insert_sorted_items(info for info in item_list 
                if self._should_show_item(info))

    def update_items(self, changed_items, already_sorted=False):
        if not already_sorted:
            self._sorter.sort_items(changed_items)
        to_add = []
        for info in changed_items:
            show = self._should_show_item(info)
            if info.id in self._iter_map:
                if not show:
                    self.remove_item(info.id)
                else:
                    self.update_item(info)
            elif show:
                to_add.append(info)
        self._insert_sorted_items(to_add)

    def remove_item(self, id):
        iter = self._iter_map.pop(id)
        self.model.remove(iter)

    def update_item(self, info):
        iter = self._iter_map[info.id]
        self.model.update_value(iter, 0, info)

    def remove_items(self, id_list):
        for id in id_list:
            self.remove_item(id)

    def set_search_text(self, search_text):
        newly_matching = self._find_newly_matching_items(search_text)
        removed = self._remove_non_matching_items(search_text)
        self._sorter.sort_items(newly_matching)
        self._insert_sorted_items(newly_matching)
        self._search_text = search_text
        for item in removed:
            self._non_matching_items[item.id] = item
        for item in newly_matching:
            del self._non_matching_items[item.id]

    def move_items(self, insert_before, item_ids):
        """Move a group of items inside the list.

        The items for item_ids will be positioned before insert_before.
        insert_before should be an iterator, or None to position the items at
        the end of the list.
        """

        new_iters = _ItemReorderer().reorder(self.model, insert_before,
                item_ids)
        self._iter_map.update(new_iters)

    def _find_newly_matching_items(self, search_text):
        retval = []
        for item in self._non_matching_items.values():
            if item_matches_search(item, search_text):
                retval.append(item)
        return retval

    def _remove_non_matching_items(self, search_text):
        removed = []
        iter = self.model.first_iter()
        while iter is not None:
            item = self.model[iter][0]
            if not item_matches_search(item, search_text):
                iter = self.model.remove(iter)
                removed.append(item)
            else:
                iter = self.model.next_iter(iter)
        return removed

class DownloadingItemList(ItemList):
    """ItemList that only displays downloading items."""
    def filter(self, item_info):
        return (item_info.download_info and 
                not item_info.download_info.finished)
        
class DownloadedItemList(ItemList):
    """ItemList that only displays downloaded items."""
    def filter(self, item_info):
        return (item_info.download_info and 
                item_info.download_info.finished)

class _ItemReorderer(object):
    """Handles re-ordering items inside an itemlist.
    
    This object is just around for utility sake.  It's only created to track
    the state during the call to ItemList.move_items()
    """

    def __init__(self):
        self.removed_rows = []

    def calc_insert_id(self, model):
        if self.insert_iter is not None:
            self.insert_id = model[self.insert_iter][0].id
        else:
            self.insert_id = None

    def reorder(self, model, insert_iter, ids):
        self.insert_iter = insert_iter
        self.calc_insert_id(model)
        self.remove_rows(model, ids)
        return self.put_rows_back(model)

    def remove_row(self, model, iter, row):
        self.removed_rows.append(row)
        if row[0].id == self.insert_id:
            self.insert_iter = model.next_iter(self.insert_iter)
            self.calc_insert_id(model)
        return model.remove(iter)

    def remove_rows(self, model, ids):
        # iterating through the entire table seems inefficient, but we have to
        # know the order of rows so we can insert them back in the right
        # order.
        iter = model.first_iter()
        while iter is not None:
            row = model[iter]
            if row[0].id in ids:
                # need to make a copy of the row data, since we're removing it
                # from the table
                iter = self.remove_row(model, iter, tuple(row))
            else:
                iter = model.next_iter(iter)

    def put_rows_back(self, model):
        if self.insert_iter is None:
            def put_back(moved_row):
                return model.append(*moved_row)
        else:
            def put_back(moved_row):
                return model.insert_before(self.insert_iter, *moved_row)
        retval = {}
        for removed_row in self.removed_rows:
            iter = put_back(removed_row)
            retval[removed_row[0].id] = iter
        return retval
